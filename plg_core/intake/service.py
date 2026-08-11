from __future__ import annotations

import json
import re
import sqlite3

from fastapi import HTTPException

from legacy_app import next_customer_number, next_job_number, next_machine_number, next_request_number
from plg_core.intake.parser import parse_intake


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _exact_customer_match(connection: sqlite3.Connection, proposal: dict):
    contact, company = _norm(proposal["contact_name"]), _norm(proposal["company_name"])
    if not (contact or company):
        return None
    for row in connection.execute("SELECT * FROM customers WHERE active=1 ORDER BY id"):
        if contact:
            if _norm(row["name"]) == contact:
                return row
        elif company and _norm(row["company"]) == company:
            return row
    return None


def _strong_machine_match(connection: sqlite3.Connection, identifier: str):
    normalized = _norm(identifier)
    if not normalized:
        return None
    row = connection.execute(
        "SELECT m.* FROM machine_identifiers i JOIN machines m ON m.id=i.machine_id "
        "WHERE REPLACE(REPLACE(LOWER(i.identifier_value),'-',''),' ','')=? AND m.active=1 LIMIT 1",
        (normalized,),
    ).fetchone()
    if row:
        return row
    for candidate in connection.execute("SELECT * FROM machines WHERE active=1 AND TRIM(vin_pin_serial)!=''"):
        if _norm(candidate["vin_pin_serial"]) == normalized:
            return candidate
    return None


def create_proposal(connection: sqlite3.Connection, raw_input: str) -> int:
    parsed = parse_intake(raw_input)
    connection.execute("BEGIN IMMEDIATE")
    cursor = connection.execute(
        """INSERT INTO intake_proposals
        (raw_input,contact_name,company_name,location,phone,email,review_state)
        VALUES (?,?,?,?,?,?,?)""",
        (parsed["raw_input"], parsed["contact_name"], parsed["company_name"],
         parsed["location"], parsed["phone"], parsed["email"], parsed["review_state"]),
    )
    proposal_id = int(cursor.lastrowid)
    customer = _exact_customer_match(connection, parsed)
    if customer:
        connection.execute("UPDATE intake_proposals SET matched_customer_id=? WHERE id=?", (customer["id"], proposal_id))
    sequence = 0
    for sequence, asset in enumerate(parsed["assets"], 1):
        asset_cursor = connection.execute(
            """INSERT INTO intake_proposal_assets
            (proposal_id,sequence,asset_category,manufacturer,model,year,market_region,
             model_code,review_state) VALUES (?,?,?,?,?,?,?,?,?)""",
            (proposal_id, sequence, asset["asset_category"], asset["manufacturer"],
             asset["model"], asset["year"], asset["market_region"], asset["model_code"],
             asset["review_state"]),
        )
        asset_id = int(asset_cursor.lastrowid)
        match = None
        for identifier in asset["identifiers"]:
            connection.execute(
                """INSERT INTO intake_proposal_identifiers
                (proposal_id,proposal_asset_id,identifier_type,identifier_value,component_label,is_primary,review_state)
                VALUES (?,?,?,?,?,?,?)""",
                (proposal_id, asset_id, identifier["identifier_type"], identifier["value"],
                 identifier["component_label"], int(identifier["primary"]), identifier["review_state"]),
            )
            if identifier["primary"]:
                match = _strong_machine_match(connection, identifier["value"])
        if match:
            connection.execute("UPDATE intake_proposal_assets SET matched_machine_id=? WHERE id=?", (match["id"], asset_id))
        for need_sequence, need in enumerate(asset["needs"], 1):
            connection.execute(
                """INSERT INTO intake_proposal_needs
                (proposal_id,proposal_asset_id,sequence,wording,original_wording,review_state)
                VALUES (?,?,?,?,?,?)""",
                (proposal_id, asset_id, need_sequence, need["wording"], need["original_wording"], need["review_state"]),
            )
    for need_sequence, need in enumerate(parsed["unassigned_needs"], 1):
        connection.execute(
            """INSERT INTO intake_proposal_needs
            (proposal_id,proposal_asset_id,sequence,wording,original_wording,review_state)
            VALUES (?,NULL,?,?,?,?)""",
            (proposal_id, need_sequence, need["wording"], need["original_wording"], need["review_state"]),
        )
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "DETERMINISTIC", json.dumps(parsed, default=str), "Original text preserved on proposal"),
    )
    connection.commit()
    return proposal_id


def load_proposal(connection: sqlite3.Connection, proposal_id: int) -> dict:
    proposal = connection.execute("SELECT * FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(status_code=404, detail="Smart Intake proposal not found.")
    result = dict(proposal)
    result["assets"] = []
    for row in connection.execute(
        "SELECT * FROM intake_proposal_assets WHERE proposal_id=? AND included=1 ORDER BY sequence,id", (proposal_id,)
    ):
        asset = dict(row)
        asset["identifiers"] = [dict(item) for item in connection.execute(
            "SELECT * FROM intake_proposal_identifiers WHERE proposal_asset_id=? ORDER BY is_primary DESC,id", (row["id"],)
        )]
        asset["needs"] = [dict(item) for item in connection.execute(
            "SELECT * FROM intake_proposal_needs WHERE proposal_asset_id=? AND included=1 ORDER BY sequence,id", (row["id"],)
        )]
        result["assets"].append(asset)
    result["unassigned_needs"] = [dict(item) for item in connection.execute(
        "SELECT * FROM intake_proposal_needs WHERE proposal_id=? AND proposal_asset_id IS NULL AND included=1 ORDER BY sequence,id",
        (proposal_id,),
    )]
    return result


def _primary_identifier(connection: sqlite3.Connection, asset_id: int):
    return connection.execute(
        "SELECT * FROM intake_proposal_identifiers WHERE proposal_asset_id=? ORDER BY is_primary DESC,id LIMIT 1",
        (asset_id,),
    ).fetchone()


def confirm_proposal(connection: sqlite3.Connection, proposal_id: int, lock_version: int) -> int:
    connection.execute("BEGIN IMMEDIATE")
    proposal = connection.execute("SELECT * FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(status_code=404, detail="Smart Intake proposal not found.")
    if proposal["status"] == "CONFIRMED" and proposal["created_job_id"]:
        connection.rollback()
        return int(proposal["created_job_id"])
    if proposal["status"] != "DRAFT":
        connection.rollback()
        raise HTTPException(status_code=409, detail="This proposal is no longer editable.")
    if int(proposal["lock_version"]) != int(lock_version):
        connection.rollback()
        raise HTTPException(status_code=409, detail="This proposal changed in another tab. Reload and review it again.")
    assets = connection.execute(
        "SELECT * FROM intake_proposal_assets WHERE proposal_id=? AND included=1 ORDER BY sequence,id", (proposal_id,)
    ).fetchall()
    needs = connection.execute(
        "SELECT * FROM intake_proposal_needs WHERE proposal_id=? AND included=1 ORDER BY sequence,id", (proposal_id,)
    ).fetchall()
    if not (proposal["contact_name"].strip() or proposal["company_name"].strip()):
        connection.rollback()
        raise HTTPException(status_code=400, detail="Enter a customer name or company before confirming.")
    customer = connection.execute("SELECT * FROM customers WHERE id=? AND active=1", (proposal["matched_customer_id"],)).fetchone() if proposal["matched_customer_id"] else None
    if customer is None:
        customer_id = int(connection.execute(
            "INSERT INTO customers (name,company,phone,email,address) VALUES (?,?,?,?,?)",
            (proposal["contact_name"] or proposal["company_name"], proposal["company_name"],
             proposal["phone"], proposal["email"], proposal["location"]),
        ).lastrowid)
        connection.execute("UPDATE customers SET customer_number=? WHERE id=?", (next_customer_number(connection), customer_id))
        customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    customer_id = int(customer["id"])
    request_id = int(connection.execute(
        """INSERT INTO customer_requests
        (request_number,request_text,individual_name,company_name,phone,email,location,
         requested_parts,customer_id,status)
        VALUES ('',?,?,?,?,?,?,?,?,'NEW')""",
        (proposal["raw_input"], proposal["contact_name"], proposal["company_name"], proposal["phone"],
         proposal["email"], proposal["location"], "\n".join(row["wording"] for row in needs), customer_id),
    ).lastrowid)
    connection.execute("UPDATE customer_requests SET request_number=? WHERE id=?", (next_request_number(connection), request_id))
    job_id = int(connection.execute(
        """INSERT INTO jobs
        (job_number,created_date,customer_id,customer,company,phone,email,address,status,notes)
        VALUES (?,DATE('now'),?,?,?,?,?,?, 'REQUESTED',?)""",
        (next_job_number(connection), customer_id, customer["name"], customer["company"] or "",
         customer["phone"] or "", customer["email"] or "", customer["address"] or "",
         f"Created from Smart Intake proposal {proposal_id}"),
    ).lastrowid)
    asset_map: dict[int, int] = {}
    first_machine_id = None
    first_asset = None
    for sequence, asset in enumerate(assets, 1):
        primary_identifier = _primary_identifier(connection, int(asset["id"]))
        machine = connection.execute("SELECT * FROM machines WHERE id=? AND active=1", (asset["matched_machine_id"],)).fetchone() if asset["matched_machine_id"] else None
        if machine and int(machine["customer_id"]) != customer_id:
            raise HTTPException(status_code=409, detail="A matched asset belongs to another customer. Review the proposal before confirming.")
        if machine is None:
            identifier_value = primary_identifier["identifier_value"] if primary_identifier else ""
            display = " ".join(value for value in (asset["manufacturer"], asset["model"]) if value).strip() or identifier_value or "Asset"
            machine_id = int(connection.execute(
                """INSERT INTO machines
                (customer_id,machine_number,registry_type,name,manufacturer,model,year,vin_pin_serial,
                 market_region,model_code,notes,active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (customer_id, next_machine_number(connection), asset["asset_category"] or "other", display,
                 asset["manufacturer"], asset["model"], asset["year"], identifier_value,
                 asset["market_region"], asset["model_code"],
                 "Operator confirmed through Smart Intake"),
            ).lastrowid)
        else:
            machine_id = int(machine["id"])
        if first_machine_id is None:
            first_machine_id = machine_id
            first_asset = asset
        job_asset_id = int(connection.execute(
            """INSERT INTO job_assets
            (job_id,machine_id,customer_id,asset_type,name,manufacturer,model,year,vin_pin_serial,
             market_region,model_code,is_primary)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, machine_id, customer_id, asset["asset_category"],
             " ".join(value for value in (asset["manufacturer"], asset["model"]) if value),
             asset["manufacturer"], asset["model"], asset["year"],
             primary_identifier["identifier_value"] if primary_identifier else "",
             asset["market_region"], asset["model_code"], int(sequence == 1)),
        ).lastrowid)
        asset_map[int(asset["id"])] = job_asset_id
        for identifier in connection.execute("SELECT * FROM intake_proposal_identifiers WHERE proposal_asset_id=?", (asset["id"],)):
            connection.execute(
                "INSERT OR IGNORE INTO machine_identifiers (machine_id,identifier_type,identifier_value,component_label,is_primary) VALUES (?,?,?,?,?)",
                (machine_id, identifier["identifier_type"], identifier["identifier_value"], identifier["component_label"], identifier["is_primary"]),
            )
    if first_machine_id and first_asset:
        primary = _primary_identifier(connection, int(first_asset["id"]))
        connection.execute(
            "UPDATE jobs SET machine_id=?,manufacturer=?,machine=?,pin_serial=? WHERE id=?",
            (first_machine_id, first_asset["manufacturer"], first_asset["model"], primary["identifier_value"] if primary else "", job_id),
        )
        connection.execute("UPDATE customer_requests SET machine_id=?,manufacturer=?,model=?,year=?,identifier=? WHERE id=?",
                           (first_machine_id, first_asset["manufacturer"], first_asset["model"], first_asset["year"], primary["identifier_value"] if primary else "", request_id))
    for need in needs:
        connection.execute(
            "INSERT INTO requested_needs (job_id,job_asset_id,customer_request_id,wording) VALUES (?,?,?,?)",
            (job_id, asset_map.get(need["proposal_asset_id"]), request_id, need["wording"]),
        )
    connection.execute("UPDATE customer_requests SET job_id=?,status='COMPLETED' WHERE id=?", (job_id, request_id))
    changed = connection.execute(
        """UPDATE intake_proposals SET status='CONFIRMED',created_request_id=?,created_job_id=?,
        confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,lock_version=lock_version+1
        WHERE id=? AND status='DRAFT' AND lock_version=?""",
        (request_id, job_id, proposal_id, lock_version),
    )
    if changed.rowcount != 1:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Another confirmation already completed.")
    connection.execute(
        "INSERT INTO job_timeline (job_id,event_type,icon,message) VALUES (?,?,?,?)",
        (job_id, "SMART_INTAKE_CONFIRMED", "", f"Smart Intake proposal {proposal_id} confirmed with {len(assets)} assets and {len(needs)} requested needs."),
    )
    connection.commit()
    return job_id
