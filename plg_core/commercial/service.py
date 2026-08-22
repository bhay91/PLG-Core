from __future__ import annotations

from contextlib import closing
from datetime import date
import sqlite3

from fastapi import HTTPException

from legacy_app import get_connection, next_quote_number
from plg_core.audit import write_audit
from plg_core.revisions.quote_workflow import (
    _insert_quote_items, _revision_quote_rows, _totals, _write_documents,
)
from plg_core.revisions.service import (
    commit_work_revision, ensure_revision_mutable, start_work_revision, touch_revision,
)
from plg_core.timeline import log_job_event


def _bill_to(job, kind: str, values: dict | None = None) -> dict:
    values = values or {}
    kind = str(kind or "CONTACT").strip().upper()
    if kind not in {"CONTACT", "COMPANY", "CUSTOM"}:
        raise HTTPException(status_code=400, detail="Invalid Bill To selection.")
    if kind == "CONTACT":
        name = str(job["customer"] or "").strip()
        company = ""
    elif kind == "COMPANY":
        company = str(job["company"] or "").strip()
        if not company:
            raise HTTPException(status_code=400, detail="This Job has no company/account name.")
        name = company
    else:
        name = str(values.get("name") or "").strip()
        company = str(values.get("company") or "").strip()
        if not name and not company:
            raise HTTPException(status_code=400, detail="Custom Bill To name or company is required.")
    return {
        "kind": kind,
        "name": name,
        "company": company,
        "address": str(values.get("address") or job["address"] or "").strip(),
        "phone": str(values.get("phone") or job["phone"] or "").strip(),
        "email": str(values.get("email") or job["email"] or "").strip(),
    }


def _create_track(connection, job_id: int, purpose: str, root_quote_id=None) -> int:
    return int(connection.execute(
        "INSERT INTO quote_tracks(job_id,root_quote_id,purpose) VALUES (?,?,?)",
        (job_id, root_quote_id, purpose),
    ).lastrowid)


def _insert_quote(
    connection, *, job, revision_id: int | None, rows, totals, bill_to,
    commercial_kind="INDEPENDENT", split_from_quote_id=None,
    supersedes_quote_id=None, track_id=None,
):
    quote_number = next_quote_number(connection)
    asset_keys = {
        (
            row["asset_manufacturer_snapshot"], row["asset_model_snapshot"],
            row["asset_serial_snapshot"],
        )
        for row in rows if row["job_asset_id"] is not None
    }
    if len(asset_keys) == 1:
        manufacturer_snapshot, machine_snapshot, serial_snapshot = next(iter(asset_keys))
    else:
        manufacturer_snapshot = job["manufacturer"] if len(asset_keys) < 2 else "Multiple"
        machine_snapshot = job["machine"] if len(asset_keys) < 2 else "Job Assets"
        serial_snapshot = job["pin_serial"] if len(asset_keys) < 2 else ""
    cursor = connection.execute(
        """
        INSERT INTO quotes (
            quote_number,job_id,quote_date,status,parts_subtotal,shipping_total,
            service_charge,sourcing_fee,customer_total,supplier_total,profit_total,
            work_revision_id,supersedes_quote_id,is_current,quote_track_id,
            commercial_kind,split_from_quote_id,bill_to_kind,
            bill_to_name_snapshot,bill_to_company_snapshot,bill_to_address_snapshot,
            bill_to_phone_snapshot,bill_to_email_snapshot,
            customer_name_snapshot,company_snapshot,phone_snapshot,email_snapshot,
            address_snapshot,manufacturer_snapshot,machine_snapshot,pin_serial_snapshot
        ) VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            quote_number, job["id"], date.today().isoformat(),
            totals["parts_subtotal"], totals["shipping_total"],
            totals["service_charge"], totals["sourcing_fee"],
            totals["customer_total"], totals["supplier_total"], totals["profit_total"],
            revision_id, supersedes_quote_id, track_id, commercial_kind,
            split_from_quote_id, bill_to["kind"], bill_to["name"], bill_to["company"],
            bill_to["address"], bill_to["phone"], bill_to["email"],
            job["customer"], job["company"], job["phone"], job["email"],
            job["address"], manufacturer_snapshot, machine_snapshot, serial_snapshot,
        ),
    )
    quote_id = int(cursor.lastrowid)
    if track_id is None:
        track_id = _create_track(connection, int(job["id"]), commercial_kind, quote_id)
        connection.execute(
            "UPDATE quotes SET quote_track_id=? WHERE id=?", (track_id, quote_id)
        )
    _insert_quote_items(connection, quote_id, rows)
    connection.execute(
        "INSERT OR IGNORE INTO quote_item_decisions(quote_item_id) "
        "SELECT id FROM quote_items WHERE quote_id=?", (quote_id,),
    )
    return quote_id


def _continue_remaining_work(job_id: int, committed_revision_id: int) -> int | None:
    with closing(get_connection()) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM work_revision_items WHERE work_revision_id=? "
            "AND generated_job_part_id IS NULL AND disposition='ACTIVE'",
            (committed_revision_id,),
        ).fetchone()[0]
    if not remaining:
        return None
    revision = start_work_revision(
        job_id, reason="Continue unquoted Job work",
        _source_revision_id=committed_revision_id,
    )
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        basket = connection.execute("SELECT id FROM baskets WHERE job_id=?", (job_id,)).fetchone()
        connection.execute(
            "DELETE FROM basket_items WHERE basket_id=? AND origin_work_revision_item_id IN "
            "(SELECT id FROM work_revision_items WHERE work_revision_id=? "
            "AND generated_job_part_id IS NOT NULL)",
            (basket["id"], committed_revision_id),
        )
        connection.execute(
            "UPDATE basket_items SET selected=0 WHERE basket_id=?", (basket["id"],)
        )
        connection.execute(
            "UPDATE work_revisions SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?", (revision["id"],)
        )
        write_audit(
            connection, action="UNQUOTED_WORK_CONTINUED", entity_type="WORK_REVISION",
            entity_id=revision["id"], summary="Created editable workspace for unquoted lines",
            metadata={"job_id": job_id, "source_revision_id": committed_revision_id},
        )
        connection.commit()
    return int(revision["id"])


def create_selective_draft_quote(
    job_id: int,
    *,
    basket_item_ids: list[int],
    bill_to_kind: str = "CONTACT",
    bill_to_values: dict | None = None,
    expected_revision_id: int,
    expected_version: int,
):
    selected_ids = sorted({int(value) for value in basket_item_ids})
    if not selected_ids:
        raise HTTPException(status_code=400, detail="Select at least one eligible line.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = ensure_revision_mutable(
            connection, job_id, expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        existing = connection.execute(
            "SELECT * FROM quotes WHERE work_revision_id=?", (revision["id"],)
        ).fetchone()
        if existing is not None:
            connection.commit()
            return dict(existing)
        basket = connection.execute("SELECT * FROM baskets WHERE job_id=?", (job_id,)).fetchone()
        placeholders = ",".join("?" for _ in selected_ids)
        actual = connection.execute(
            f"SELECT id FROM basket_items WHERE basket_id=? AND id IN ({placeholders})",
            (basket["id"], *selected_ids),
        ).fetchall()
        if len(actual) != len(selected_ids):
            raise HTTPException(status_code=409, detail="One or more selected lines are stale.")
        connection.execute("UPDATE basket_items SET selected=0 WHERE basket_id=?", (basket["id"],))
        connection.execute(
            f"UPDATE basket_items SET selected=1 WHERE basket_id=? AND id IN ({placeholders})",
            (basket["id"], *selected_ids),
        )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()
        next_version = int(revision["lock_version"]) + 1
        revision_id = int(revision["id"])

    commit_work_revision(
        job_id, expected_revision_id=revision_id, expected_version=next_version,
    )
    with closing(get_connection()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM quotes WHERE work_revision_id=?", (revision_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing)
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            revision = connection.execute(
                "SELECT * FROM work_revisions WHERE id=? AND state='COMMITTED'", (revision_id,)
            ).fetchone()
            rows = _revision_quote_rows(connection, revision_id)
            totals = _totals(connection, revision, rows)
            bill_to = _bill_to(job, bill_to_kind, bill_to_values)
            quote_id = _insert_quote(
                connection, job=job, revision_id=revision_id, rows=rows,
                totals=totals, bill_to=bill_to,
            )
            connection.execute("UPDATE jobs SET status='QUOTED' WHERE id=?", (job_id,))
            write_audit(
                connection, action="SELECTIVE_DRAFT_QUOTE_CREATED", entity_type="QUOTE",
                entity_id=quote_id, summary="Selective draft quote created",
                metadata={"job_id": job_id, "work_revision_id": revision_id,
                          "selected_basket_item_ids": selected_ids},
            )
            log_job_event(
                connection, job_id=job_id, event_type="SELECTIVE_DRAFT_QUOTE_CREATED",
                icon="📄", message="Draft quote created from selected Job lines",
            )
            from plg_core.requests.service import archive_originating_requests_for_quote
            archive_originating_requests_for_quote(connection, job_id=job_id, quote_id=quote_id)
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            winner = connection.execute(
                "SELECT * FROM quotes WHERE work_revision_id=?", (revision_id,)
            ).fetchone()
            if winner is not None:
                return dict(winner)
            raise HTTPException(status_code=409, detail="Quote creation conflicted; reload the Job.") from error
    _write_documents(quote_id)
    _continue_remaining_work(job_id, revision_id)
    with closing(get_connection()) as connection:
        return dict(connection.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone())


def update_draft_bill_to(quote_id: int, *, kind: str, values: dict | None = None):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        quote = connection.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if quote is None:
            raise HTTPException(status_code=404, detail="Quote not found.")
        if str(quote["status"]).upper() != "DRAFT" or quote["issued_at"]:
            raise HTTPException(status_code=409, detail="Issued quotes require a governed successor.")
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (quote["job_id"],)).fetchone()
        bill = _bill_to(job, kind, values)
        connection.execute(
            "UPDATE quotes SET bill_to_kind=?,bill_to_name_snapshot=?,"
            "bill_to_company_snapshot=?,bill_to_address_snapshot=?,"
            "bill_to_phone_snapshot=?,bill_to_email_snapshot=?,content_version=content_version+1 "
            "WHERE id=? AND status='DRAFT'",
            (bill["kind"],bill["name"],bill["company"],bill["address"],
             bill["phone"],bill["email"],quote_id),
        )
        write_audit(
            connection, action="DRAFT_BILL_TO_CHANGED", entity_type="QUOTE",
            entity_id=quote_id, summary="Draft Quote Bill To changed",
            metadata={"bill_to_kind": bill["kind"]},
        )
        connection.commit()
    _write_documents(quote_id)


def _copy_quote_items(connection, source_quote_id: int, quote_id: int, item_ids: list[int]):
    placeholders=",".join("?" for _ in item_ids)
    rows=connection.execute(
        f"SELECT * FROM quote_items WHERE quote_id=? AND id IN ({placeholders}) ORDER BY id",
        (source_quote_id,*item_ids),
    ).fetchall()
    if len(rows)!=len(item_ids):
        raise HTTPException(status_code=409,detail="Split selection contains stale quote lines.")
    new_ids=[]
    columns=[
        "part_id","source_id","job_asset_id","origin_work_revision_item_id","primary_requested_need_id","quantity",
        "description","internal_part_number","supplier_name","source_type","brand",
        "supplier_part_number","supplier_unit_cost","customer_unit_price",
        "supplier_line_total","customer_line_total","line_profit","pricing_mode",
        "customer_unit_price_override","recommended_markup_percent","research_session_id",
        "research_evidence","research_notes","identified_at","asset_name_snapshot",
        "asset_type_snapshot","asset_manufacturer_snapshot","asset_model_snapshot",
        "asset_year_snapshot","asset_serial_snapshot",
    ]
    marks=",".join("?" for _ in range(len(columns)+1))
    for row in rows:
        new_ids.append(int(connection.execute(
            f"INSERT INTO quote_items(quote_id,{','.join(columns)}) VALUES ({marks})",
            (quote_id,*(row[column] for column in columns)),
        ).lastrowid))
    return rows,new_ids


def split_issued_quote(source_quote_id: int, *, groups: list[dict], reason: str):
    reason=str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400,detail="Split reason is required.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        source=connection.execute("SELECT * FROM quotes WHERE id=?",(source_quote_id,)).fetchone()
        if source is None: raise HTTPException(status_code=404,detail="Quote not found.")
        if not int(source["is_current"] or 0):
            existing=connection.execute("SELECT q.* FROM quote_split_successors s JOIN quote_splits x ON x.id=s.split_id JOIN quotes q ON q.id=s.successor_quote_id WHERE x.source_quote_id=? ORDER BY q.id",(source_quote_id,)).fetchall()
            if existing: return [dict(row) for row in existing]
            raise HTTPException(status_code=409,detail="Quote is no longer current.")
        if str(source["status"]).upper() not in {"SENT","REJECTED","APPROVED","REVISION_REQUIRED"}:
            raise HTTPException(status_code=409,detail="Only an issued current quote can be split.")
        if len(groups)<1:
            raise HTTPException(status_code=400,detail="At least one successor group is required.")
        if connection.execute("SELECT 1 FROM invoices WHERE quote_id=?",(source_quote_id,)).fetchone():
            raise HTTPException(status_code=409,detail="An invoiced quote cannot use ordinary split workflow.")
        all_ids=[]
        for group in groups: all_ids.extend(int(v) for v in group.get("quote_item_ids",[]))
        if len(all_ids)!=len(set(all_ids)) or not all_ids:
            raise HTTPException(status_code=400,detail="Each selected line may appear in only one successor.")
        job=connection.execute("SELECT * FROM jobs WHERE id=?",(source["job_id"],)).fetchone()
        split_id=int(connection.execute("INSERT INTO quote_splits(source_quote_id,reason) VALUES (?,?)",(source_quote_id,reason)).lastrowid)
        successors=[]
        for index,group in enumerate(groups):
            ids=[int(v) for v in group.get("quote_item_ids",[])]
            if not ids: continue
            bill=_bill_to(job,group.get("bill_to_kind") or source["bill_to_kind"],group.get("bill_to_values"))
            rows=connection.execute("SELECT * FROM quote_items WHERE id IN ("+",".join("?" for _ in ids)+")",ids).fetchall()
            parts=sum(float(r["customer_line_total"] or 0) for r in rows); supplier=sum(float(r["supplier_line_total"] or 0) for r in rows)
            service=float(source["service_charge"] or 0) if index==0 else 0; sourcing=float(source["sourcing_fee"] or 0) if index==0 else 0; shipping=float(source["shipping_total"] or 0) if index==0 else 0
            totals={"parts_subtotal":parts,"shipping_total":shipping,"service_charge":service,"sourcing_fee":sourcing,"customer_total":parts+shipping+service+sourcing,"supplier_total":supplier+shipping,"profit_total":parts+service+sourcing-supplier}
            track=_create_track(connection,int(source["job_id"]),"SPLIT_SUCCESSOR")
            qid=_insert_quote(connection,job=job,revision_id=None,rows=[],totals=totals,bill_to=bill,commercial_kind="SPLIT_SUCCESSOR",split_from_quote_id=source_quote_id,supersedes_quote_id=source_quote_id,track_id=track)
            connection.execute("UPDATE quote_tracks SET root_quote_id=? WHERE id=?",(qid,track))
            old_rows,new_ids=_copy_quote_items(connection,source_quote_id,qid,ids)
            asset_keys={(r["asset_manufacturer_snapshot"],r["asset_model_snapshot"],r["asset_serial_snapshot"]) for r in old_rows if r["job_asset_id"] is not None}
            if len(asset_keys)==1:
                manufacturer,machine,serial=next(iter(asset_keys))
                connection.execute("UPDATE quotes SET manufacturer_snapshot=?,machine_snapshot=?,pin_serial_snapshot=? WHERE id=?",(manufacturer,machine,serial,qid))
            elif len(asset_keys)>1:
                connection.execute("UPDATE quotes SET manufacturer_snapshot='Multiple',machine_snapshot='Job Assets',pin_serial_snapshot='' WHERE id=?",(qid,))
            connection.execute("INSERT INTO quote_split_successors(split_id,successor_quote_id) VALUES (?,?)",(split_id,qid))
            for old,new in zip(old_rows,new_ids):
                decision=connection.execute("SELECT decision FROM quote_item_decisions WHERE quote_item_id=?",(old["id"],)).fetchone()
                disposition=(decision[0] if decision and decision[0]!="PENDING" else "MOVED")
                connection.execute("INSERT INTO quote_item_lineage(split_id,predecessor_quote_item_id,successor_quote_item_id,successor_quote_id,disposition,quantity) VALUES (?,?,?,?,?,?)",(split_id,old["id"],new,qid,disposition,old["quantity"]))
            connection.execute("INSERT OR IGNORE INTO quote_item_decisions(quote_item_id) SELECT id FROM quote_items WHERE quote_id=?",(qid,))
            successors.append(qid)
        connection.execute("UPDATE quotes SET status='SUPERSEDED',is_current=0,superseded_at=CURRENT_TIMESTAMP,supersession_reason=? WHERE id=? AND is_current=1",(reason,source_quote_id))
        write_audit(connection,action="ISSUED_QUOTE_SPLIT",entity_type="QUOTE",entity_id=source_quote_id,summary=f"Issued quote split into {len(successors)} successor draft(s)",metadata={"split_id":split_id,"successors":successors,"reason":reason})
        log_job_event(connection,job_id=int(source["job_id"]),event_type="ISSUED_QUOTE_SPLIT",icon="⑂",message=f"{source['quote_number']} split into {len(successors)} successor quotes")
        connection.commit()
    for qid in successors: _write_documents(qid)
    with closing(get_connection()) as connection:
        return [dict(connection.execute("SELECT * FROM quotes WHERE id=?",(qid,)).fetchone()) for qid in successors]


def record_quote_item_decisions(quote_id: int, decisions: dict[int,str], *, reason=""):
    with closing(get_connection()) as connection:
        quote=connection.execute("SELECT * FROM quotes WHERE id=?",(quote_id,)).fetchone()
        if quote is None: raise HTTPException(status_code=404,detail="Quote not found.")
        if str(quote["status"]).upper() not in {"SENT","REJECTED","APPROVED","REVISION_REQUIRED"}:
            raise HTTPException(status_code=409,detail="Line decisions require an issued quote.")
        for item_id,decision in decisions.items():
            decision=str(decision).upper()
            if decision not in {"PENDING","ACCEPTED","DECLINED","DEFERRED"}: raise HTTPException(status_code=400,detail="Invalid line decision.")
            item=connection.execute("SELECT quantity FROM quote_items WHERE id=? AND quote_id=?",(item_id,quote_id)).fetchone()
            if item is None: raise HTTPException(status_code=404,detail="Quote line not found.")
            connection.execute("INSERT INTO quote_item_decisions(quote_item_id,decision,accepted_quantity,reason,decided_at) VALUES (?,?,?,?,CASE WHEN ?='PENDING' THEN NULL ELSE CURRENT_TIMESTAMP END) ON CONFLICT(quote_item_id) DO UPDATE SET decision=excluded.decision,accepted_quantity=excluded.accepted_quantity,reason=excluded.reason,decided_at=excluded.decided_at,updated_at=CURRENT_TIMESTAMP",(item_id,decision,item["quantity"] if decision=="ACCEPTED" else 0,str(reason or "").strip(),decision))
        write_audit(connection,action="QUOTE_LINE_DECISIONS_RECORDED",entity_type="QUOTE",entity_id=quote_id,summary="Partial customer decisions recorded",metadata={"decisions":{str(k):v for k,v in decisions.items()}})
        connection.commit()
