from __future__ import annotations

from contextlib import closing
import sqlite3

from fastapi import HTTPException

from legacy_app import get_connection, next_machine_number
from plg_core.audit import write_audit
from plg_core.revisions.service import ensure_revision_mutable, touch_revision
from plg_core.timeline import log_job_event


def _job(connection: sqlite3.Connection, job_id: int):
    job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def _asset(connection: sqlite3.Connection, job_id: int, asset_id: int):
    asset = connection.execute(
        "SELECT * FROM job_assets WHERE id=? AND job_id=?", (asset_id, job_id)
    ).fetchone()
    if asset is None:
        raise HTTPException(status_code=404, detail="Job asset not found.")
    return asset


def list_job_assets(connection: sqlite3.Connection, job_id: int, *, include_archived=False):
    return connection.execute(
        "SELECT a.*,m.machine_number FROM job_assets a "
        "LEFT JOIN machines m ON m.id=a.machine_id "
        "WHERE a.job_id=? " + ("" if include_archived else "AND a.state='ACTIVE' ") +
        "ORDER BY a.is_primary DESC,a.id",
        (job_id,),
    ).fetchall()


def add_job_asset(
    job_id: int,
    *,
    manufacturer: str = "",
    model: str = "",
    year: str = "",
    vin_pin_serial: str = "",
    asset_type: str = "",
    name: str = "",
    notes: str = "",
    machine_id: int | None = None,
    make_primary: bool = False,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = _job(connection, job_id)
        machine = None
        if machine_id is not None:
            machine = connection.execute(
                "SELECT * FROM machines WHERE id=?", (machine_id,)
            ).fetchone()
            if machine is None:
                raise HTTPException(status_code=404, detail="Machine Registry record not found.")
            if job["customer_id"] is not None and int(machine["customer_id"]) != int(job["customer_id"]):
                raise HTTPException(
                    status_code=409,
                    detail="This machine belongs to a different customer account.",
                )
            manufacturer = machine["manufacturer"] or manufacturer
            model = machine["model"] or machine["name"] or model
            year = machine["year"] or year
            vin_pin_serial = machine["vin_pin_serial"] or vin_pin_serial
            asset_type = machine["registry_type"] or asset_type
            name = machine["name"] or name
        else:
            normalized = any(
                str(value or "").strip()
                for value in (manufacturer, model, year, vin_pin_serial, name)
            )
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail="Enter at least a manufacturer, model, name, year, or VIN/PIN/serial.",
                )
            machine_number = next_machine_number(connection)
            display_name = str(name or "").strip() or " ".join(
                value for value in (str(manufacturer).strip(), str(model).strip()) if value
            ) or str(vin_pin_serial).strip() or "Asset"
            machine_id = int(connection.execute(
                """
                INSERT INTO machines (
                    customer_id,machine_number,name,manufacturer,model,year,
                    vin_pin_serial,registry_type,notes,active
                ) VALUES (?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    job["customer_id"], machine_number, display_name,
                    str(manufacturer).strip(), str(model).strip(), str(year).strip(),
                    str(vin_pin_serial).strip(), str(asset_type).strip() or "asset",
                    str(notes).strip(),
                ),
            ).lastrowid)
            name = display_name
        existing_count = int(connection.execute(
            "SELECT COUNT(*) FROM job_assets WHERE job_id=? AND state='ACTIVE'", (job_id,)
        ).fetchone()[0])
        make_primary = bool(make_primary or existing_count == 0)
        if make_primary:
            connection.execute("UPDATE job_assets SET is_primary=0 WHERE job_id=?", (job_id,))
        try:
            asset_id = int(connection.execute(
                """
                INSERT INTO job_assets (
                    job_id,machine_id,customer_id,asset_type,name,manufacturer,
                    model,year,vin_pin_serial,notes,is_primary
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, machine_id, job["customer_id"], str(asset_type or "").strip(),
                    str(name or "").strip(), str(manufacturer or "").strip(),
                    str(model or "").strip(), str(year or "").strip(),
                    str(vin_pin_serial or "").strip(), str(notes or "").strip(),
                    int(make_primary),
                ),
            ).lastrowid)
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = connection.execute(
                "SELECT * FROM job_assets WHERE job_id=? AND machine_id=?",
                (job_id, machine_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            raise
        message = f"Asset added: {str(manufacturer or '').strip()} {str(model or '').strip()}".strip()
        write_audit(
            connection, action="JOB_ASSET_ADDED", entity_type="JOB_ASSET",
            entity_id=asset_id, summary=message,
            metadata={"job_id": job_id, "machine_id": machine_id, "primary": make_primary},
        )
        log_job_event(
            connection, job_id=job_id, event_type="JOB_ASSET_ADDED", icon="▱",
            message=message,
        )
        connection.commit()
        return dict(connection.execute("SELECT * FROM job_assets WHERE id=?", (asset_id,)).fetchone())


def edit_job_asset(job_id: int, asset_id: int, **changes):
    allowed = {"asset_type", "name", "manufacturer", "model", "year", "vin_pin_serial", "notes"}
    payload = {key: str(value or "").strip() for key, value in changes.items() if key in allowed}
    if not payload:
        raise HTTPException(status_code=400, detail="No asset changes were supplied.")
    with closing(get_connection()) as connection:
        asset = _asset(connection, job_id, asset_id)
        historical = connection.execute(
            "SELECT 1 FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id "
            "WHERE qi.job_asset_id=? AND q.status!='DRAFT' LIMIT 1", (asset_id,)
        ).fetchone()
        identity = set(payload) - {"notes"}
        if historical and identity:
            raise HTTPException(
                status_code=409,
                detail="Issued commercial history protects this asset identity. Notes may still be updated.",
            )
        assignments=", ".join(f"{key}=?" for key in payload)
        connection.execute(
            f"UPDATE job_assets SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*payload.values(), asset_id),
        )
        write_audit(
            connection, action="JOB_ASSET_EDITED", entity_type="JOB_ASSET",
            entity_id=asset_id, summary="Job asset updated",
            metadata={"job_id": job_id, "fields": sorted(payload)},
        )
        log_job_event(
            connection, job_id=job_id, event_type="JOB_ASSET_EDITED", icon="▱",
            message="Job asset details updated",
        )
        connection.commit()
        return dict(connection.execute("SELECT * FROM job_assets WHERE id=?", (asset_id,)).fetchone())


def set_primary_job_asset(job_id: int, asset_id: int):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        asset = _asset(connection, job_id, asset_id)
        if asset["state"] != "ACTIVE":
            raise HTTPException(status_code=409, detail="Archived assets cannot be primary.")
        connection.execute("UPDATE job_assets SET is_primary=0 WHERE job_id=?", (job_id,))
        connection.execute("UPDATE job_assets SET is_primary=1 WHERE id=?", (asset_id,))
        connection.commit()
        return dict(connection.execute("SELECT * FROM job_assets WHERE id=?", (asset_id,)).fetchone())


def archive_job_asset(job_id: int, asset_id: int, *, reason: str):
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Archive reason is required.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        asset = _asset(connection, job_id, asset_id)
        durable = connection.execute(
            """
            SELECT 1 FROM (
                SELECT job_asset_id FROM quote_items
                UNION ALL SELECT job_asset_id FROM invoice_items
                UNION ALL SELECT job_asset_id FROM supplier_order_items
            ) history WHERE job_asset_id=? LIMIT 1
            """, (asset_id,),
        ).fetchone()
        if durable:
            raise HTTPException(
                status_code=409,
                detail="This asset appears in durable quote, invoice, or order history and cannot be removed.",
            )
        if connection.execute(
            "SELECT 1 FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE b.job_id=? AND bi.job_asset_id=? LIMIT 1", (job_id, asset_id)
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Reassign or remove this asset's active parts before archiving it.",
            )
        connection.execute(
            "UPDATE job_assets SET state='ARCHIVED',is_primary=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (asset_id,),
        )
        if not connection.execute(
            "SELECT 1 FROM job_assets WHERE job_id=? AND state='ACTIVE' AND is_primary=1",
            (job_id,),
        ).fetchone():
            connection.execute(
                "UPDATE job_assets SET is_primary=1 WHERE id=(SELECT id FROM job_assets "
                "WHERE job_id=? AND state='ACTIVE' ORDER BY id LIMIT 1)", (job_id,)
            )
        write_audit(
            connection, action="JOB_ASSET_ARCHIVED", entity_type="JOB_ASSET",
            entity_id=asset_id, summary=f"Job asset archived: {reason}",
            metadata={"job_id": job_id, "reason": reason},
        )
        log_job_event(
            connection, job_id=job_id, event_type="JOB_ASSET_ARCHIVED", icon="▱",
            message=f"Job asset archived: {reason}",
        )
        connection.commit()


def assign_basket_item_asset(
    job_id: int,
    item_id: int,
    asset_id: int | None,
    *,
    expected_revision_id: int | None,
    expected_version: int | None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT bi.*,b.job_id FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (item_id, job_id),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Part not found in this Job.")
        revision = ensure_revision_mutable(
            connection, job_id, expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        if asset_id is not None:
            asset = _asset(connection, job_id, asset_id)
            if asset["state"] != "ACTIVE":
                raise HTTPException(status_code=409, detail="Select an active Job asset.")
        connection.execute(
            "UPDATE basket_items SET job_asset_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (asset_id, item_id),
        )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()
