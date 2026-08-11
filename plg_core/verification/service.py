from __future__ import annotations

from contextlib import closing

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.revisions.service import ensure_revision_mutable
from plg_core.timeline import log_job_event


def start_asset_research(
    job_id: int,
    job_asset_id: int,
    connector_profile_id: int,
    *,
    requested_need_id: int | None = None,
    expected_revision_id: int | None,
    expected_version: int | None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_revision_mutable(
            connection, job_id, expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        asset = connection.execute(
            "SELECT * FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
            (job_asset_id, job_id),
        ).fetchone()
        if asset is None:
            raise HTTPException(status_code=404, detail="Selected Job asset is unavailable.")
        if requested_need_id is not None and connection.execute(
            "SELECT 1 FROM requested_needs WHERE id=? AND job_id=? AND job_asset_id=?",
            (requested_need_id, job_id, job_asset_id),
        ).fetchone() is None:
            raise HTTPException(status_code=409, detail="Requested Need does not belong to the selected asset.")
        connector = connection.execute(
            "SELECT * FROM connector_profiles WHERE id=? AND is_enabled=1 AND is_archived=0",
            (connector_profile_id,),
        ).fetchone()
        if connector is None:
            raise HTTPException(status_code=404, detail="Verification source is unavailable.")
        applicability = {value.strip().lower() for value in str(connector["manufacturer_applicability"] or "").split(",") if value.strip()}
        if applicability and str(asset["manufacturer"] or "").strip().lower() not in applicability:
            raise HTTPException(status_code=409, detail="This source is not configured for the selected manufacturer.")
        connection.execute(
            "UPDATE verification_sessions SET status='CANCELLED',completed_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND job_asset_id=? AND basket_item_id IS NULL AND status='ACTIVE'",
            (job_id, job_asset_id),
        )
        session_id = int(connection.execute(
            "INSERT INTO verification_sessions(job_id,job_asset_id,requested_need_id,connector_profile_id) "
            "VALUES (?,?,?,?)",
            (job_id, job_asset_id, requested_need_id, connector_profile_id),
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO active_source_import (
                id,job_id,job_asset_id,requested_need_id,source_key,source_name,
                verification_session_id,activated_at
            ) VALUES (1,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                job_id=excluded.job_id,job_asset_id=excluded.job_asset_id,
                requested_need_id=excluded.requested_need_id,source_key=excluded.source_key,
                source_name=excluded.source_name,verification_session_id=excluded.verification_session_id,
                basket_item_id=NULL,job_part_id=NULL,activated_at=CURRENT_TIMESTAMP
            """,
            (job_id, job_asset_id, requested_need_id, connector["connector_key"],
             connector["display_name"], session_id),
        )
        message = f"Research started for {asset['manufacturer']} {asset['model']} using {connector['display_name']}"
        write_audit(connection, action="ASSET_RESEARCH_STARTED", entity_type="VERIFICATION_SESSION",
                    entity_id=session_id, summary=message,
                    metadata={"job_id": job_id, "job_asset_id": job_asset_id,
                              "requested_need_id": requested_need_id})
        log_job_event(connection, job_id=job_id, event_type="ASSET_RESEARCH_STARTED",
                      icon="⌕", message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM verification_sessions WHERE id=?", (session_id,)).fetchone())


def start_part_verification(
    job_id: int,
    basket_item_id: int,
    connector_profile_id: int,
    *,
    expected_revision_id: int | None,
    expected_version: int | None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT bi.*,b.job_id FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (basket_item_id, job_id),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Requested part not found in this Job.")
        ensure_revision_mutable(
            connection, job_id, expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        assets = connection.execute(
            "SELECT COUNT(*) FROM job_assets WHERE job_id=? AND state='ACTIVE'", (job_id,)
        ).fetchone()[0]
        if assets > 1 and item["job_asset_id"] is None:
            raise HTTPException(
                status_code=409,
                detail="Assign this part to a Job asset before starting verification.",
            )
        connector = connection.execute(
            "SELECT * FROM connector_profiles WHERE id=? AND is_enabled=1 AND is_archived=0",
            (connector_profile_id,),
        ).fetchone()
        if connector is None:
            raise HTTPException(status_code=404, detail="Verification source is unavailable.")
        if item["job_asset_id"] is not None:
            asset = connection.execute(
                "SELECT * FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
                (item["job_asset_id"], job_id),
            ).fetchone()
            if asset is None:
                raise HTTPException(status_code=409, detail="The assigned Job asset is unavailable.")
            applicability = str(connector["manufacturer_applicability"] or "").strip()
            allowed = {part.strip().lower() for part in applicability.split(",") if part.strip()}
            if allowed and str(asset["manufacturer"] or "").strip().lower() not in allowed:
                raise HTTPException(
                    status_code=409,
                    detail="This verification source is not configured for the assigned manufacturer.",
                )
        connection.execute(
            "UPDATE verification_sessions SET status='CANCELLED',completed_at=CURRENT_TIMESTAMP "
            "WHERE basket_item_id=? AND status='ACTIVE'", (basket_item_id,),
        )
        session_id = int(connection.execute(
            """
            INSERT INTO verification_sessions (
                job_id,job_asset_id,basket_item_id,connector_profile_id
            ) VALUES (?,?,?,?)
            """,
            (job_id,item["job_asset_id"],basket_item_id,connector_profile_id),
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO active_source_import (
                id,job_id,source_key,source_name,activated_at,
                job_asset_id,basket_item_id,verification_session_id
            ) VALUES (1,?,?,?,CURRENT_TIMESTAMP,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                job_id=excluded.job_id,source_key=excluded.source_key,
                source_name=excluded.source_name,activated_at=excluded.activated_at,
                job_asset_id=excluded.job_asset_id,basket_item_id=excluded.basket_item_id,
                verification_session_id=excluded.verification_session_id
            """,
            (
                job_id,connector["connector_key"],connector["display_name"],
                item["job_asset_id"],basket_item_id,session_id,
            ),
        )
        message=f"Verification started for {item['requested_description']} using {connector['display_name']}"
        write_audit(
            connection,action="PART_VERIFICATION_STARTED",entity_type="VERIFICATION_SESSION",
            entity_id=session_id,summary=message,
            metadata={"job_id":job_id,"job_asset_id":item["job_asset_id"],
                      "basket_item_id":basket_item_id,"connector_profile_id":connector_profile_id},
        )
        log_job_event(
            connection,job_id=job_id,event_type="PART_VERIFICATION_STARTED",icon="✓",
            message=message,
        )
        connection.commit()
        return dict(connection.execute(
            "SELECT * FROM verification_sessions WHERE id=?",(session_id,)
        ).fetchone())
