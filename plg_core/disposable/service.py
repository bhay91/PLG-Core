from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from legacy_app import UPLOADS_DIR, get_connection
from plg_core.audit import write_audit


UPLOAD_ROOT = UPLOADS_DIR
DELETE_PHRASE = "DELETE TEST DATA"


def _rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _ids(connection: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> list[int]:
    return [int(row[0]) for row in connection.execute(f"SELECT id FROM {table} WHERE {where}", params)]


def _count(connection: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def _marks(values: list[int]) -> str:
    return ",".join("?" for _ in values) or "NULL"


def _plan_token(plan: dict[str, Any]) -> str:
    snapshot = {
        "root_type": plan["root_type"], "root_id": plan["root_id"],
        "request_updated_at": plan.get("request_updated_at", ""),
        "proposal_versions": plan.get("proposal_versions", []),
        "revision_versions": plan.get("revision_versions", []),
        "job_state": plan.get("job_state", ""), "counts": plan["counts"],
        "blockers": plan["blockers"],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _base_plan() -> dict[str, Any]:
    return {
        "blockers": [], "counts": {}, "files": [], "proposal_ids": [],
        "proposal_versions": [], "revision_versions": [], "machines": [], "needs": [],
        "customer": "", "request_number": "", "job_number": "", "job_id": None,
    }


def _add_blocker(plan: dict[str, Any], label: str, count: int) -> None:
    if count:
        plan["blockers"].append(f"{label} ({count})")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _request_plan(connection: sqlite3.Connection, request_id: int) -> dict[str, Any]:
    request = connection.execute("SELECT * FROM customer_requests WHERE id=?", (request_id,)).fetchone()
    if request is None:
        raise HTTPException(status_code=404, detail="Customer request not found.")
    plan = _base_plan()
    plan.update({
        "root_type": "REQUEST", "root_id": request_id,
        "request_id": request_id, "request_number": str(request["request_number"] or ""),
        "request_updated_at": str(request["updated_at"] or ""),
        "customer_id": request["customer_id"],
        "customer": str(request["company_name"] or request["individual_name"] or "Customer unknown"),
        "job_id": int(request["job_id"]) if request["job_id"] else None,
    })
    if plan["job_id"] is None:
        plan["blockers"].append("no linked Job; use normal request archive/cancel history")
        plan["token"] = _plan_token(plan)
        return plan
    job_id = int(plan["job_id"])
    job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        plan["blockers"].append("linked Job is missing")
        plan["token"] = _plan_token(plan)
        return plan
    plan.update({"job_number": str(job["job_number"] or ""), "job_state": str(job["status"] or ""),
                 "customer_id": job["customer_id"] or request["customer_id"],
                 "customer": str(job["company"] or job["customer"] or plan["customer"])})
    plan["proposal_ids"] = _ids(
        connection, "intake_proposals", "created_request_id=? OR created_job_id=?", (request_id, job_id)
    )
    if plan["proposal_ids"]:
        plan["proposal_versions"] = [
            [int(row["id"]), int(row["lock_version"]), str(row["status"])] for row in
            _rows(connection, "SELECT id,lock_version,status FROM intake_proposals WHERE id IN (" +
                  _marks(plan["proposal_ids"]) + ") ORDER BY id", tuple(plan["proposal_ids"]))
        ]
    asset_rows = _rows(connection, "SELECT * FROM job_assets WHERE job_id=? ORDER BY id", (job_id,))
    asset_ids = [int(row["id"]) for row in asset_rows]
    machine_ids = sorted({int(row["machine_id"]) for row in asset_rows if row.get("machine_id")})
    plan["machines"] = [" ".join(filter(None, [row.get("manufacturer"), row.get("model") or row.get("name")])).strip()
                        or str(row.get("vin_pin_serial") or "Machine") for row in asset_rows]
    need_rows = _rows(connection, "SELECT id,wording FROM requested_needs WHERE job_id=? ORDER BY id", (job_id,))
    need_ids = [int(row["id"]) for row in need_rows]
    plan["needs"] = [str(row["wording"]) for row in need_rows]
    basket_ids = _ids(connection, "baskets", "job_id=?", (job_id,))
    basket_item_ids = _ids(connection, "basket_items", f"basket_id IN ({_marks(basket_ids)})", tuple(basket_ids)) if basket_ids else []
    part_ids = _ids(connection, "job_parts", "job_id=?", (job_id,))
    revision_rows = _rows(connection, "SELECT id,state,lock_version,based_on_quote_id FROM work_revisions WHERE job_id=?", (job_id,))
    revision_ids = [int(row["id"]) for row in revision_rows]
    revision_item_ids = _ids(connection, "work_revision_items", f"work_revision_id IN ({_marks(revision_ids)})", tuple(revision_ids)) if revision_ids else []
    plan["revision_versions"] = [[int(row["id"]), int(row["lock_version"]), str(row["state"])] for row in revision_rows]

    # Commercial or durable history always wins over apparent test naming.
    blocker_specs = [
        ("quotes", "quotes", "job_id=?", (job_id,)),
        ("invoices (including voided)", "invoices", "job_id=?", (job_id,)),
        ("payments/refunds/adjustments", "customer_transactions", "job_id=?", (job_id,)),
        ("supplier orders", "supplier_orders", "job_id=?", (job_id,)),
        ("deliveries", "deliveries", "job_id=?", (job_id,)),
        ("shipments", "consolidated_shipments", "job_id=?", (job_id,)),
        ("external opportunities/references", "opportunities", "converted_job_id=? OR customer_request_id=?", (job_id, request_id)),
        ("committed machine-parts history", "machine_parts_history", "original_job_number=?", (plan["job_number"],)),
    ]
    for label, table, where, params in blocker_specs:
        if _table_exists(connection, table):
            _add_blocker(plan, label, _count(connection, table, where, params))
    committed = sum(1 for row in revision_rows if str(row["state"]).upper() == "COMMITTED" or row.get("based_on_quote_id"))
    _add_blocker(plan, "committed or quote-based Work Revisions", committed)
    if _table_exists(connection, "quote_documents_manifest"):
        issued = connection.execute(
            "SELECT COUNT(*) FROM quote_documents_manifest d JOIN quotes q ON q.id=d.quote_id WHERE q.job_id=?",
            (job_id,),
        ).fetchone()[0]
        _add_blocker(plan, "issued quote documents", int(issued))

    plan["ids"] = {"assets": asset_ids, "needs": need_ids, "baskets": basket_ids,
                   "basket_items": basket_item_ids, "parts": part_ids,
                   "revisions": revision_ids, "revision_items": revision_item_ids,
                   "machines": machine_ids}
    count_specs = {
        "Smart Intake proposals": len(plan["proposal_ids"]), "Requests": 1, "Jobs": 1,
        "Machines in Job": len(machine_ids), "Job Assets": len(asset_ids),
        "Requested Needs": len(need_ids), "Parts Found": len(basket_item_ids),
        "Job Parts": len(part_ids), "Work Revisions": len(revision_ids),
        "Research sessions": _count(connection, "verification_sessions", "job_id=?", (job_id,)),
        "Research proposals": _count(connection, "research_capture_proposals", "job_id=?", (job_id,)),
        "Follow-ups": _count(connection, "job_follow_ups", "job_id=?", (job_id,)),
        "Timeline": _count(connection, "job_timeline", "job_id=?", (job_id,)),
    }
    plan["counts"] = count_specs
    file_queries = [
        ("customer_request_attachments", "file_path", "request_id=?", (request_id,)),
        ("basket_attachments", "file_path", f"basket_id IN ({_marks(basket_ids)})", tuple(basket_ids)),
        ("work_revision_attachments", "file_path", f"work_revision_id IN ({_marks(revision_ids)})", tuple(revision_ids)),
    ]
    for table, column, where, params in file_queries:
        if params and _table_exists(connection, table):
            plan["files"].extend(str(row[0]) for row in connection.execute(f"SELECT {column} FROM {table} WHERE {where}", params) if row[0])
    if plan["proposal_ids"]:
        plan["files"].extend(str(row[0]) for row in connection.execute(
            f"SELECT stored_path FROM intake_proposal_attachments WHERE proposal_id IN ({_marks(plan['proposal_ids'])})",
            tuple(plan["proposal_ids"]),
        ) if row[0])
    plan["counts"]["Attachments"] = len(plan["files"])
    plan["exclusive_machine_ids"] = _exclusive_machines(connection, machine_ids, job_id, request_id)
    plan["exclusive_customer_id"] = _exclusive_customer(
        connection, int(plan["customer_id"]) if plan.get("customer_id") else None,
        job_id, request_id, plan["exclusive_machine_ids"], plan["proposal_ids"],
    )
    plan["blockers"].extend(_unplanned_dependency_blockers(connection, plan))
    plan["token"] = _plan_token(plan)
    return plan


def _proposal_plan(connection: sqlite3.Connection, proposal_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Smart Intake proposal not found.")
    plan = _base_plan()
    plan.update({"root_type": "PROPOSAL", "root_id": proposal_id, "proposal_ids": [proposal_id],
                 "proposal_versions": [[proposal_id, int(row["lock_version"]), str(row["status"])]],
                 "customer": str(row["company_name"] or row["contact_name"] or "Sender unknown"),
                 "request_number": f"Smart Intake #{proposal_id}", "job_state": str(row["status"] or "")})
    if row["created_request_id"] or row["created_job_id"]:
        plan["blockers"].append("proposal is linked to confirmed business work")
    if str(row["status"]).upper() not in {"DRAFT", "CANCELLED"}:
        plan["blockers"].append("proposal is not draft or cancelled")
    plan["machines"] = [str(r[0]).strip() for r in connection.execute(
        "SELECT TRIM(manufacturer || ' ' || model) FROM intake_proposal_assets WHERE proposal_id=?", (proposal_id,)
    )]
    plan["needs"] = [str(r[0]) for r in connection.execute(
        "SELECT wording FROM intake_proposal_needs WHERE proposal_id=?", (proposal_id,)
    )]
    plan["files"] = [str(r[0]) for r in connection.execute(
        "SELECT stored_path FROM intake_proposal_attachments WHERE proposal_id=?", (proposal_id,)
    ) if r[0]]
    plan["counts"] = {"Smart Intake proposals": 1, "Requests": 0, "Jobs": 0,
                      "Machines in proposal": len(plan["machines"]), "Requested Needs": len(plan["needs"]),
                      "Parts Found": 0, "Attachments": len(plan["files"])}
    plan["token"] = _plan_token(plan)
    return plan


def _exclusive_machines(connection: sqlite3.Connection, machine_ids: list[int], job_id: int, request_id: int) -> list[int]:
    exclusive = []
    for machine_id in machine_ids:
        checks = [
            ("job_assets", "machine_id=? AND job_id!=?", (machine_id, job_id)),
            ("jobs", "machine_id=? AND id!=?", (machine_id, job_id)),
            ("customer_requests", "machine_id=? AND id!=?", (machine_id, request_id)),
            ("machine_parts_history", "machine_id=?", (machine_id,)),
            ("machine_ownership_history", "machine_id=?", (machine_id,)),
            ("opportunity_machines", "machine_id=?", (machine_id,)),
        ]
        if not any(_table_exists(connection, t) and _count(connection, t, w, p) for t, w, p in checks):
            exclusive.append(machine_id)
    return exclusive


def _exclusive_customer(connection: sqlite3.Connection, customer_id: int | None, job_id: int,
                        request_id: int, machine_ids: list[int], proposal_ids: list[int]) -> int | None:
    if not customer_id:
        return None
    checks = [
        ("jobs", "customer_id=? AND id!=?", (customer_id, job_id)),
        ("customer_requests", "customer_id=? AND id!=?", (customer_id, request_id)),
        ("machines", f"customer_id=? AND id NOT IN ({_marks(machine_ids)})", (customer_id, *machine_ids)),
        ("customer_transactions", "customer_id=?", (customer_id,)),
        ("opportunities", "customer_id=?", (customer_id,)),
        ("machine_ownership_history", "from_customer_id=? OR to_customer_id=?", (customer_id, customer_id)),
        ("intake_proposals", f"matched_customer_id=? AND id NOT IN ({_marks(proposal_ids)})", (customer_id, *proposal_ids)),
    ]
    return None if any(_table_exists(connection, t) and _count(connection, t, w, p) for t, w, p in checks) else customer_id


def _unplanned_dependency_blockers(connection: sqlite3.Connection, plan: dict[str, Any]) -> list[str]:
    """Fail closed when a schema extension points at a record this plan would delete."""
    ids = plan["ids"]
    targets: dict[str, list[int]] = {
        "jobs": [int(plan["job_id"])], "customer_requests": [int(plan["request_id"])],
        "intake_proposals": list(plan["proposal_ids"]), "job_assets": ids["assets"],
        "requested_needs": ids["needs"], "baskets": ids["baskets"],
        "basket_items": ids["basket_items"], "job_parts": ids["parts"],
        "work_revisions": ids["revisions"], "work_revision_items": ids["revision_items"],
        "machines": list(plan["exclusive_machine_ids"]),
        "customers": [int(plan["exclusive_customer_id"])] if plan["exclusive_customer_id"] else [],
    }
    # Every table here is either explicitly deleted, deliberately preserved, or an explicit blocker above.
    accounted = {
        "active_job_import", "active_source_import", "active_verification", "basket_activity",
        "basket_attachments", "basket_item_need_links", "basket_items", "basket_sources", "baskets",
        "consolidated_shipments", "customer_locations", "customer_request_attachments", "customer_requests",
        "customer_transactions", "deliveries", "intake_proposal_assets", "intake_proposal_attachments",
        "intake_proposal_contributions", "intake_proposal_identifiers", "intake_proposal_needs",
        "intake_proposals", "invoices", "job_assets", "job_follow_ups", "job_parts", "job_timeline",
        "jobs", "machine_identifiers", "machine_ownership_history", "machine_parts_history", "machines",
        "opportunities", "opportunity_machines", "part_shipping_data", "part_sources", "quote_documents_manifest",
        "invoice_documents_manifest",
        "quote_item_decisions", "quote_item_lineage", "quote_items", "quote_split_successors", "quote_splits",
        "quote_tracks", "quotes", "requested_needs", "research_capture_proposals", "source_cart_imports",
        "supplier_order_items", "supplier_orders", "receiving_event_items", "receiving_events", "deliveries",
        "delivery_items", "verification_sessions", "work_revision_attachments", "work_revision_item_need_links",
        "work_revision_items", "work_revision_sources", "work_revisions",
    }
    blockers: list[str] = []
    tables = [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    for child in tables:
        for fk in connection.execute(f'PRAGMA foreign_key_list("{child}")'):
            parent, child_column = str(fk[2]), str(fk[3])
            parent_ids = targets.get(parent, [])
            if not parent_ids or child in accounted:
                continue
            count = connection.execute(
                f'SELECT COUNT(*) FROM "{child}" WHERE "{child_column}" IN ({_marks(parent_ids)})',
                tuple(parent_ids),
            ).fetchone()[0]
            if count:
                blockers.append(f"unplanned dependency {child}.{child_column} ({int(count)})")
    return blockers


def build_request_deletion_plan(request_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    if connection is not None:
        return _request_plan(connection, request_id)
    with closing(get_connection()) as owned:
        return _request_plan(owned, request_id)


def build_proposal_deletion_plan(proposal_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    if connection is not None:
        return _proposal_plan(connection, proposal_id)
    with closing(get_connection()) as owned:
        return _proposal_plan(owned, proposal_id)


def _validate_confirmation(plan: dict[str, Any], reason: str, confirmation_number: str,
                           confirmation_phrase: str, expected_token: str) -> str:
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Deletion reason is required.")
    expected_number = plan["job_number"] or plan["request_number"]
    if str(confirmation_number or "").strip() != expected_number:
        raise HTTPException(status_code=400, detail=f"Type {expected_number} exactly to confirm deletion.")
    if str(confirmation_phrase or "").strip() != DELETE_PHRASE:
        raise HTTPException(status_code=400, detail=f"Type {DELETE_PHRASE} exactly to confirm deletion.")
    if expected_token != plan["token"]:
        raise HTTPException(status_code=409, detail="This record changed after review. Reload the deletion review.")
    if plan["blockers"]:
        raise HTTPException(status_code=409, detail="Permanent deletion is blocked by " + ", ".join(plan["blockers"]) + ".")
    return reason


def _safe_paths(paths: list[str]) -> list[Path]:
    safe: list[Path] = []
    for raw in paths:
        path = Path(raw).resolve()
        try:
            path.relative_to(UPLOAD_ROOT)
        except ValueError:
            raise HTTPException(status_code=409, detail="An attachment path is outside the approved PPS upload root.")
        safe.append(path)
    return safe


def _delete_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent != UPLOAD_ROOT and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            # Database deletion is authoritative; a later maintenance pass may remove an inaccessible orphan file.
            pass


def delete_disposable_request(request_id: int, *, reason: str, confirmation_number: str,
                              confirmation_phrase: str, expected_token: str) -> dict[str, Any]:
    connection = get_connection()
    files: list[Path] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _request_plan(connection, request_id)
        reason = _validate_confirmation(plan, reason, confirmation_number, confirmation_phrase, expected_token)
        files = _safe_paths(plan["files"])
        _delete_request_chain(connection, plan, reason, confirmation_number)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _delete_files(files)
    return {"deleted": True, "entity_number": confirmation_number}


def delete_disposable_proposal(proposal_id: int, *, reason: str, confirmation_number: str,
                               confirmation_phrase: str, expected_token: str) -> dict[str, Any]:
    connection = get_connection()
    files: list[Path] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _proposal_plan(connection, proposal_id)
        reason = _validate_confirmation(plan, reason, confirmation_number, confirmation_phrase, expected_token)
        files = _safe_paths(plan["files"])
        metadata = {"root": "PROPOSAL", "proposal_ids": plan["proposal_ids"], "counts": plan["counts"],
                    "reason": reason, "operator_confirmation": confirmation_phrase}
        connection.execute(
            "INSERT INTO deletion_tombstones(entity_type,entity_number,former_entity_id,reason,metadata_json) VALUES ('DISPOSABLE_PROPOSAL',?,?,?,?)",
            (plan["request_number"], proposal_id, reason, json.dumps(metadata, sort_keys=True)),
        )
        write_audit(connection, action="DISPOSABLE_RECORD_DELETED", entity_type="DELETION_TOMBSTONE",
                    entity_id=plan["request_number"], summary=f"Disposable {plan['request_number']} permanently deleted.", metadata=metadata)
        connection.execute("DELETE FROM intake_proposals WHERE id=?", (proposal_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _delete_files(files)
    return {"deleted": True, "entity_number": confirmation_number}


def _delete_request_chain(connection: sqlite3.Connection, plan: dict[str, Any], reason: str,
                          confirmation_number: str) -> None:
    job_id, request_id = int(plan["job_id"]), int(plan["request_id"])
    ids = plan["ids"]
    metadata = {"root": "REQUEST_JOB", "request_id": request_id, "request_number": plan["request_number"],
                "job_id": job_id, "job_number": plan["job_number"], "proposal_ids": plan["proposal_ids"],
                "counts": plan["counts"], "reason": reason, "operator_confirmation": DELETE_PHRASE}
    connection.execute(
        "INSERT INTO deletion_tombstones(entity_type,entity_number,former_entity_id,reason,metadata_json) VALUES ('DISPOSABLE_JOB',?,?,?,?)",
        (plan["job_number"], job_id, reason, json.dumps(metadata, sort_keys=True)),
    )
    write_audit(connection, action="DISPOSABLE_RECORD_DELETED", entity_type="DELETION_TOMBSTONE",
                entity_id=plan["job_number"], summary=f"Disposable Job {plan['job_number']} permanently deleted. Reason: {reason}", metadata=metadata)

    # Explicit reverse-dependency order. Commercial tables are absent by preflight.
    connection.execute("UPDATE jobs SET active_work_revision_id=NULL WHERE id=?", (job_id,))
    connection.execute("DELETE FROM active_source_import WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM active_verification WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM active_job_import WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM research_capture_proposals WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM job_follow_ups WHERE job_id=?", (job_id,))
    if ids["revision_items"]:
        connection.execute(f"DELETE FROM part_shipping_data WHERE work_revision_item_id IN ({_marks(ids['revision_items'])})", tuple(ids["revision_items"]))
        connection.execute(f"DELETE FROM work_revision_item_need_links WHERE work_revision_item_id IN ({_marks(ids['revision_items'])})", tuple(ids["revision_items"]))
    if ids["revisions"]:
        params = tuple(ids["revisions"])
        connection.execute(f"DELETE FROM work_revision_attachments WHERE work_revision_id IN ({_marks(ids['revisions'])})", params)
        connection.execute(f"DELETE FROM work_revision_items WHERE work_revision_id IN ({_marks(ids['revisions'])})", params)
        connection.execute(f"DELETE FROM work_revision_sources WHERE work_revision_id IN ({_marks(ids['revisions'])})", params)
        connection.execute(f"DELETE FROM work_revisions WHERE id IN ({_marks(ids['revisions'])})", params)
    if ids["parts"]:
        params = tuple(ids["parts"])
        connection.execute(f"DELETE FROM part_shipping_data WHERE job_part_id IN ({_marks(ids['parts'])})", params)
        connection.execute(f"DELETE FROM part_sources WHERE part_id IN ({_marks(ids['parts'])})", params)
        connection.execute(f"DELETE FROM job_parts WHERE id IN ({_marks(ids['parts'])})", params)
    if ids["basket_items"]:
        params = tuple(ids["basket_items"])
        connection.execute(f"DELETE FROM part_shipping_data WHERE basket_item_id IN ({_marks(ids['basket_items'])})", params)
        connection.execute(f"DELETE FROM basket_item_need_links WHERE basket_item_id IN ({_marks(ids['basket_items'])})", params)
    if ids["baskets"]:
        connection.execute(f"DELETE FROM baskets WHERE id IN ({_marks(ids['baskets'])})", tuple(ids["baskets"]))
    connection.execute("DELETE FROM verification_sessions WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM source_cart_imports WHERE job_id=?", (job_id,))
    if plan["proposal_ids"]:
        connection.execute(f"DELETE FROM intake_proposals WHERE id IN ({_marks(plan['proposal_ids'])})", tuple(plan["proposal_ids"]))
    if ids["needs"]:
        connection.execute(f"DELETE FROM requested_needs WHERE id IN ({_marks(ids['needs'])})", tuple(ids["needs"]))
    connection.execute("DELETE FROM job_timeline WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM customer_request_attachments WHERE request_id=?", (request_id,))
    connection.execute("DELETE FROM customer_requests WHERE id=?", (request_id,))
    connection.execute("DELETE FROM job_assets WHERE job_id=?", (job_id,))
    connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    for machine_id in plan["exclusive_machine_ids"]:
        connection.execute("DELETE FROM machine_identifiers WHERE machine_id=?", (machine_id,))
        connection.execute("DELETE FROM machines WHERE id=?", (machine_id,))
    if plan["exclusive_customer_id"]:
        connection.execute("DELETE FROM customer_locations WHERE customer_id=?", (plan["exclusive_customer_id"],))
        connection.execute("DELETE FROM customers WHERE id=?", (plan["exclusive_customer_id"],))
