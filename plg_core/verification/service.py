from __future__ import annotations

from contextlib import closing

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.revisions.service import ensure_revision_mutable
from plg_core.timeline import log_job_event
from plg_core.sources.service import list_sources_for_context, validate_source_url


def start_asset_research(
    job_id: int,
    job_asset_id: int,
    connector_profile_id: int,
    *,
    requested_need_id: int | None = None,
    expected_revision_id: int | None,
    expected_version: int | None,
    one_time_url: str = "",
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
        open_needs = connection.execute(
            "SELECT id FROM requested_needs WHERE job_id=? AND job_asset_id=? AND state='OPEN' ORDER BY id",
            (job_id, job_asset_id),
        ).fetchall()
        if requested_need_id is None and len(open_needs) == 1:
            requested_need_id = int(open_needs[0]["id"])
        if len(open_needs) > 1 and requested_need_id is None:
            raise HTTPException(
                status_code=409,
                detail="Choose the specific Customer Need before starting research for this machine.",
            )
        connector = connection.execute(
            "SELECT * FROM connector_profiles WHERE id=? AND is_enabled=1 AND is_archived=0",
            (connector_profile_id,),
        ).fetchone()
        if connector is None:
            raise HTTPException(status_code=404, detail="Verification source is unavailable.")
        available_sources = list_sources_for_context(
            connection, manufacturer=asset["manufacturer"] or "",
            asset_category=asset["asset_type"] or "other",
            market=asset["market_region"] or "UNKNOWN",
        )
        if int(connector_profile_id) not in {int(source["id"]) for source in available_sources}:
            # Legacy directory entries remain explicitly usable for backward
            # compatibility, but are not auto-recommended for unrelated assets.
            legacy_unscoped = (
                str(connector["source_type"] or "OTHER").upper() == "OTHER"
                and str(connector["provenance"] or "LEGACY").upper() == "LEGACY"
                and not str(connector["manufacturer_applicability"] or "").strip()
                and not str(connector["asset_category_applicability"] or "").strip()
                and not str(connector["market_applicability"] or "").strip()
            )
            if not legacy_unscoped:
                raise HTTPException(status_code=409, detail="This source is not configured for the selected manufacturer.")
        one_time_url = validate_source_url(one_time_url) if one_time_url else ""
        validate_source_url(connector["launch_url"] or "")
        existing = connection.execute(
            """SELECT * FROM verification_sessions
               WHERE job_id=? AND job_asset_id=?
                 AND COALESCE(requested_need_id,0)=COALESCE(?,0)
                 AND connector_profile_id=?
                 AND COALESCE(source_url_snapshot,'')=? AND status='ACTIVE'
               ORDER BY id DESC LIMIT 1""",
            (job_id, job_asset_id, requested_need_id, connector_profile_id,
             one_time_url or (connector["launch_url"] or "")),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return dict(existing)
        request_row = connection.execute(
            "SELECT id FROM customer_requests WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        need_row = connection.execute(
            "SELECT wording FROM requested_needs WHERE id=?", (requested_need_id,)
        ).fetchone() if requested_need_id is not None else None
        identifier = connection.execute(
            """SELECT mi.identifier_type,mi.identifier_value
               FROM machine_identifiers mi
               WHERE mi.machine_id=? ORDER BY mi.is_primary DESC,mi.id LIMIT 1""",
            (asset["machine_id"],),
        ).fetchone() if asset["machine_id"] else None
        identifier_type = identifier["identifier_type"] if identifier else ""
        identifier_value = identifier["identifier_value"] if identifier else (asset["vin_pin_serial"] or "")
        connection.execute(
            "UPDATE verification_sessions SET status='CANCELLED',completed_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND job_asset_id=? AND basket_item_id IS NULL AND status='ACTIVE'",
            (job_id, job_asset_id),
        )
        session_id = int(connection.execute(
            """INSERT INTO verification_sessions(
                job_id,job_asset_id,requested_need_id,connector_profile_id,
                customer_request_id,identifier_type_snapshot,identifier_value_snapshot,
                asset_snapshot,need_wording_snapshot,source_name_snapshot,
                source_url_snapshot,source_type_snapshot
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, job_asset_id, requested_need_id, connector_profile_id,
             request_row["id"] if request_row else None, identifier_type, identifier_value,
             " ".join(value for value in (asset["year"], asset["manufacturer"], asset["model"] or asset["name"]) if value).strip(),
             need_row["wording"] if need_row else "General research",
             "One-time Website" if one_time_url else connector["display_name"],
             one_time_url or (connector["launch_url"] or ""),
             "GENERAL_RESEARCH" if one_time_url else (connector["source_type"] or "OTHER")),
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
             "One-time Website" if one_time_url else connector["display_name"], session_id),
        )
        need_label = need_row["wording"] if need_row else "general research"
        source_label = "one-time website" if one_time_url else connector["display_name"]
        message = f"Research started for {need_label} on {asset['manufacturer']} {asset['model']} using {source_label}"
        write_audit(connection, action="ASSET_RESEARCH_STARTED", entity_type="VERIFICATION_SESSION",
                    entity_id=session_id, summary=message,
                    metadata={"job_id": job_id, "job_asset_id": job_asset_id,
                              "requested_need_id": requested_need_id})
        log_job_event(connection, job_id=job_id, event_type="ASSET_RESEARCH_STARTED",
                      icon="⌕", message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM verification_sessions WHERE id=?", (session_id,)).fetchone())


def start_one_time_research(
    job_id: int,
    job_asset_id: int,
    url: str,
    *,
    requested_need_id: int | None = None,
    expected_revision_id: int | None,
    expected_version: int | None,
):
    url = validate_source_url(url, allow_blank=False)
    with closing(get_connection()) as connection:
        fallback = connection.execute(
            """SELECT id FROM connector_profiles
               WHERE source_type='GENERAL_RESEARCH' AND is_enabled=1 AND is_archived=0
               ORDER BY id LIMIT 1"""
        ).fetchone()
    if fallback is None:
        raise HTTPException(status_code=409, detail="General Research is unavailable.")
    return start_asset_research(
        job_id, job_asset_id, int(fallback["id"]), requested_need_id=requested_need_id,
        expected_revision_id=expected_revision_id, expected_version=expected_version,
        one_time_url=url,
    )


def start_extension_one_time_research(
    *, job_id: int, page_url: str, job_asset_id: int | None = None,
    requested_need_id: int | None = None,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    """Turn the active extension context into a one-time website session.

    The active PPS research context is authoritative. Browser-supplied IDs may
    confirm that context, but cannot redirect a capture to a different Job,
    machine, or Requested Need.
    """
    page_url = validate_source_url(page_url, allow_blank=False)
    with closing(get_connection()) as connection:
        active = connection.execute(
            """SELECT * FROM active_source_import WHERE id=1 AND job_id=?""",
            (job_id,),
        ).fetchone()
        if active is None or active["job_asset_id"] is None:
            raise HTTPException(
                status_code=409,
                detail="Open a Job machine research context in PPS before capturing this website.",
            )
        active_asset_id = int(active["job_asset_id"])
        active_need_id = active["requested_need_id"]
        if job_asset_id is not None and int(job_asset_id) != active_asset_id:
            raise HTTPException(status_code=409, detail="Browser machine context does not match PPS.")
        if requested_need_id is not None and requested_need_id != active_need_id:
            raise HTTPException(status_code=409, detail="Browser Requested Need does not match PPS.")
    return start_one_time_research(
        job_id, active_asset_id, page_url,
        requested_need_id=active_need_id,
        expected_revision_id=expected_revision_id, expected_version=expected_version,
    )


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
