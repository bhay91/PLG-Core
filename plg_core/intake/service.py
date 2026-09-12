from __future__ import annotations

import json
import hashlib
import tempfile
import re
import sqlite3
from pathlib import Path

from fastapi import HTTPException, UploadFile

from legacy_app import next_customer_number, next_job_number, next_machine_number, next_request_number
from plg_core.audit import write_audit
from plg_core.intake.identifiers import classify_identifier, normalize_identifier
from plg_core.intake.parser import parse_intake
from plg_core.intake.attachments import ValidatedImage, copy_proposal_images_to_request, store_proposal_images, validate_attachments
from plg_core.intake.research_import import ResearchImportPackage, unpack_ppsresearch
from plg_core.intake.documents import (
    classify_document, supplier_invoice_fields, supplier_quote_fields,
)
from plg_core.basket.service import get_or_create_basket
from plg_core.revisions.service import ensure_initial_revision


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _norm_company(value: str) -> str:
    aliases = {
        "dev": "development", "development": "development",
        "co": "company", "company": "company",
        "inc": "incorporated", "incorporated": "incorporated",
        "ltd": "limited", "limited": "limited",
    }
    tokens = re.findall(r"[a-z0-9]+", str(value or "").lower())
    return "".join(aliases.get(token, token) for token in tokens)


def _candidate(row) -> dict:
    return {
        "id": int(row["id"]), "number": row["customer_number"] or "",
        "name": row["name"] or "", "company": row["company"] or "",
    }


def _customer_match(
    connection: sqlite3.Connection, proposal: dict, source_text: str,
    selected_customer_id: int | None = None, force_new: bool = False,
) -> dict:
    rows = connection.execute("SELECT * FROM customers WHERE active=1 ORDER BY id").fetchall()
    if force_new:
        return {"state": "NEW", "matched_id": None, "candidates": [], "rationale": "Operator explicitly selected a new customer."}
    if selected_customer_id:
        selected = next((row for row in rows if int(row["id"]) == int(selected_customer_id)), None)
        if selected:
            return {"state": "MATCHED", "matched_id": int(selected["id"]), "candidates": [_candidate(selected)], "rationale": "Operator selected this existing customer."}
    customer_number = ""
    number_match = re.search(r"\b(PPS-C-\d{4,}|CUST(?:OMER)?[- ]?\d+)\b", source_text, re.I)
    if number_match:
        customer_number = number_match.group(1)
        found = [row for row in rows if _norm(row["customer_number"]) == _norm(customer_number)]
        if len(found) == 1:
            return {"state": "MATCHED", "matched_id": int(found[0]["id"]), "candidates": [_candidate(found[0])], "rationale": "Exact customer number match."}
        if len(found) > 1:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [_candidate(row) for row in found], "rationale": "Customer number matched multiple active records."}
    email = str(proposal.get("email") or "").strip().lower()
    if email:
        found = [row for row in rows if str(row["email"] or "").strip().lower() == email]
        if len(found) == 1:
            return {"state": "MATCHED", "matched_id": int(found[0]["id"]), "candidates": [_candidate(found[0])], "rationale": "Unique exact email match."}
        if len(found) > 1:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [_candidate(row) for row in found], "rationale": "Email matched multiple active customers."}
    phone = _norm(proposal.get("phone") or "")
    if phone:
        found = [row for row in rows if _norm(row["phone"]) == phone]
        if len(found) == 1:
            return {"state": "MATCHED", "matched_id": int(found[0]["id"]), "candidates": [_candidate(found[0])], "rationale": "Unique normalized phone match."}
        if len(found) > 1:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [_candidate(row) for row in found], "rationale": "Phone matched multiple active customers."}
    contact, company = _norm(proposal.get("contact_name")), _norm_company(proposal.get("company_name"))
    if contact and company:
        found = [row for row in rows if _norm(row["name"]) == contact and _norm_company(row["company"]) == company]
        if len(found) == 1:
            return {"state": "MATCHED", "matched_id": int(found[0]["id"]), "candidates": [_candidate(found[0])], "rationale": "Unique customer name and company match."}
        if found:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [_candidate(row) for row in found], "rationale": "Name and company matched multiple records."}
    if contact:
        found = [row for row in rows if _norm(row["name"]) == contact]
        if found:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [_candidate(row) for row in found], "rationale": "Name-only matches require operator selection."}
    return {"state": "NEW", "matched_id": None, "candidates": [], "rationale": "No existing customer match was found."}


def _machine_candidates(connection, identifier: str) -> list:
    wanted = _norm(identifier)
    found = {}
    if not wanted:
        return []
    for row in connection.execute("SELECT * FROM machines WHERE active=1 ORDER BY id"):
        if _norm(row["vin_pin_serial"]) == wanted:
            found[int(row["id"])] = row
    for row in connection.execute(
        "SELECT m.*,i.identifier_value AS matched_identifier FROM machine_identifiers i JOIN machines m ON m.id=i.machine_id WHERE m.active=1 ORDER BY m.id"
    ):
        if _norm(row["matched_identifier"]) == wanted:
            found[int(row["id"])] = row
    return list(found.values())


def _machine_match(
    connection, asset: dict, customer_match: dict,
    selected_machine_id: int | None = None, force_new: bool = False,
) -> dict:
    primary = next((item for item in asset.get("identifiers", []) if item.get("primary") or item.get("is_primary")), None)
    identifier = str((primary or {}).get("value") or (primary or {}).get("identifier_value") or "")
    customer_id = customer_match.get("matched_id") if customer_match.get("state") == "MATCHED" else None
    if force_new:
        return {"state": "NEW", "matched_id": None, "candidates": [],
                "rationale": "Operator explicitly selected a new machine."}
    if selected_machine_id:
        selected = connection.execute(
            "SELECT * FROM machines WHERE id=? AND active=1", (selected_machine_id,)
        ).fetchone()
        if selected is None:
            return {"state": "CONFLICT", "matched_id": None, "candidates": [],
                    "rationale": "The operator-selected machine is unavailable."}
        candidate = {"id": int(selected["id"]), "customer_id": int(selected["customer_id"]),
                     "manufacturer": selected["manufacturer"] or "", "model": selected["model"] or "",
                     "identifier": selected["vin_pin_serial"] or ""}
        if not customer_id:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": [candidate],
                    "rationale": "Resolve the customer before selecting this existing machine."}
        if int(selected["customer_id"]) != int(customer_id):
            return {"state": "CONFLICT", "matched_id": None, "candidates": [candidate],
                    "rationale": "The operator-selected machine belongs to another customer."}
        return {"state": "MATCHED", "matched_id": int(selected["id"]), "candidates": [candidate],
                "rationale": "Operator selected this existing machine."}
    exact = _machine_candidates(connection, identifier)
    candidates = [{"id": int(row["id"]), "customer_id": int(row["customer_id"]), "manufacturer": row["manufacturer"] or "", "model": row["model"] or "", "identifier": row["vin_pin_serial"] or ""} for row in exact]
    if len(exact) > 1:
        return {"state": "AMBIGUOUS", "matched_id": None, "candidates": candidates, "rationale": "Identifier matched multiple active machines."}
    if len(exact) == 1:
        row = exact[0]
        if customer_id and int(row["customer_id"]) != int(customer_id):
            return {"state": "CONFLICT", "matched_id": None, "candidates": candidates, "rationale": "Exact identifier belongs to another customer."}
        if not customer_id:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": candidates, "rationale": "Identifier matched a machine but customer identity is unresolved."}
        return {"state": "MATCHED", "matched_id": int(row["id"]), "candidates": candidates, "rationale": "Exact normalized VIN/PIN/serial match."}
    manufacturer, model = _norm(asset.get("manufacturer")), _norm(asset.get("model"))
    if customer_id and manufacturer and model:
        same = connection.execute("SELECT * FROM machines WHERE active=1 AND customer_id=? ORDER BY id", (customer_id,)).fetchall()
        same = [row for row in same if _norm(row["manufacturer"]) == manufacturer and _norm(row["model"] or row["name"]) == model]
        candidates = [{"id": int(row["id"]), "customer_id": int(row["customer_id"]), "manufacturer": row["manufacturer"] or "", "model": row["model"] or "", "identifier": row["vin_pin_serial"] or ""} for row in same]
        if identifier and same:
            return {"state": "CONFLICT", "matched_id": None, "candidates": candidates, "rationale": "Same customer and model exist with a different identifier."}
        if len(same) == 1:
            return {"state": "MATCHED", "matched_id": int(same[0]["id"]), "candidates": candidates, "rationale": "Unique customer, manufacturer, and model match."}
        if len(same) > 1:
            return {"state": "AMBIGUOUS", "matched_id": None, "candidates": candidates, "rationale": "Multiple machines match customer, manufacturer, and model."}
    return {"state": "NEW", "matched_id": None, "candidates": [], "rationale": "No existing machine match was found."}


def _existing_pps_document(connection, classification: dict) -> dict | None:
    number = classification.get("document_number") or ""
    if classification["document_type"] == "PPS_QUOTE":
        row = connection.execute(
            "SELECT q.id,q.quote_number,q.status,j.id job_id,j.job_number,j.customer FROM quotes q JOIN jobs j ON j.id=q.job_id WHERE UPPER(q.quote_number)=UPPER(?)",
            (number,),
        ).fetchone()
        if row:
            return {"kind": "QUOTE", "id": int(row["id"]), "number": row["quote_number"], "status": row["status"], "job_id": int(row["job_id"]), "job_number": row["job_number"], "customer": row["customer"], "url": f"/quotes/{row['id']}/documents"}
    if classification["document_type"] == "PPS_INVOICE":
        row = connection.execute(
            "SELECT i.id,i.invoice_number,i.status,j.id job_id,j.job_number,j.customer FROM invoices i JOIN jobs j ON j.id=i.job_id WHERE UPPER(i.invoice_number)=UPPER(?)",
            (number,),
        ).fetchone()
        if row:
            return {"kind": "INVOICE", "id": int(row["id"]), "number": row["invoice_number"], "status": row["status"], "job_id": int(row["job_id"]), "job_number": row["job_number"], "customer": row["customer"], "url": f"/invoices/{row['id']}/documents"}
    return None


def _analysis(
    connection, parsed: dict, source_text: str, classification: dict,
    selected_customer_id: int | None = None, force_new_customer: bool = False,
    machine_resolutions: dict[str, object] | None = None,
) -> dict:
    customer = _customer_match(connection, parsed, source_text, selected_customer_id, force_new_customer)
    machine_resolutions = machine_resolutions or {}
    machines = []
    for asset in parsed.get("assets", []):
        resolution = machine_resolutions.get(str(asset.get("id") or ""))
        machines.append(_machine_match(
            connection, asset, customer,
            selected_machine_id=int(resolution) if str(resolution or "").isdigit() else None,
            force_new=resolution == "NEW",
        ))
    existing = _existing_pps_document(connection, classification)
    pps_missing = classification["document_type"] in {"PPS_QUOTE", "PPS_INVOICE"} and not existing
    blockers = []
    if classification["document_type"] in {"PPS_QUOTE", "PPS_INVOICE", "SUPPLIER_QUOTE", "SUPPLIER_INVOICE", "OTHER"}:
        blockers.append(f"{classification['document_type']} is review-only in Smart Intake.")
    if pps_missing:
        blockers.append("The PPS-looking document number was not found.")
    if customer["state"] in {"AMBIGUOUS", "CONFLICT"}:
        blockers.append("Customer match requires operator resolution.")
    if any(item["state"] in {"AMBIGUOUS", "CONFLICT"} for item in machines):
        blockers.append("Machine match requires operator resolution.")
    if classification["document_type"] == "SUPPLIER_INVOICE":
        supplier = supplier_invoice_fields(source_text)
    elif classification["document_type"] == "SUPPLIER_QUOTE":
        supplier = supplier_quote_fields(source_text)
    else:
        supplier = None
    if supplier and supplier.get("job_number"):
        job = connection.execute("SELECT id FROM jobs WHERE job_number=?", (supplier["job_number"],)).fetchone()
        supplier["job_url"] = f"/jobs/{job['id']}/basket?view=advanced" if job else ""
        if job:
            needs = connection.execute("SELECT id,wording FROM requested_needs WHERE job_id=? ORDER BY id", (job["id"],)).fetchall()
            for line in supplier.get("lines") or []:
                matches = [need for need in needs if _norm(need["wording"]) == _norm(line.get("description"))]
                if len(matches) == 1:
                    line["possible_requested_need"] = {"id": int(matches[0]["id"]), "wording": matches[0]["wording"]}
    if supplier and supplier.get("request_number"):
        request = connection.execute("SELECT id FROM customer_requests WHERE request_number=?", (supplier["request_number"],)).fetchone()
        supplier["request_url"] = f"/requests/{request['id']}" if request else ""
    return {
        "classification": classification, "customer_match": customer,
        "machine_matches": machines, "existing_document": existing,
        "supplier_document": supplier, "blockers": blockers,
        "machine_match_state": "NOT_IDENTIFIED" if not parsed.get("assets") else None,
        "confirm_allowed": not blockers,
    }


def refresh_proposal_analysis(connection: sqlite3.Connection, proposal_id: int) -> dict:
    proposal = connection.execute("SELECT * FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not proposal:
        raise HTTPException(status_code=404, detail="Smart Intake proposal not found.")
    assets = []
    for row in connection.execute("SELECT * FROM intake_proposal_assets WHERE proposal_id=? AND included=1 ORDER BY sequence,id", (proposal_id,)):
        asset = dict(row)
        asset["identifiers"] = [dict(item) for item in connection.execute(
            "SELECT * FROM intake_proposal_identifiers WHERE proposal_asset_id=? ORDER BY is_primary DESC,id", (row["id"],)
        )]
        assets.append(asset)
    parsed = dict(proposal)
    parsed["assets"] = assets
    source_documents = []
    previous_classification = None
    resolutions = {}
    for row in connection.execute("SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? ORDER BY id DESC", (proposal_id,)):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("smart_intake_2"):
            source_documents = payload.get("documents") or []
            previous_classification = payload["smart_intake_2"].get("classification")
            resolutions = payload.get("resolutions") or resolutions
            break
        if payload.get("resolutions") and not resolutions:
            resolutions = payload["resolutions"]
    extracted = "\n\n".join(item.get("text", "") for item in source_documents if item.get("text"))
    source_text = "\n\n".join(value for value in (proposal["raw_input"], extracted) if value).strip()
    filenames = " ".join(item.get("filename", "") for item in source_documents)
    classification = previous_classification or classify_document(source_text, filenames)
    analysis = _analysis(
        connection, parsed, source_text, classification,
        int(proposal["matched_customer_id"]) if proposal["matched_customer_id"] else None,
        resolutions.get("customer") == "NEW",
        resolutions.get("machines") or {},
    )
    connection.execute(
        "UPDATE intake_proposal_assets SET matched_machine_id=NULL WHERE proposal_id=?", (proposal_id,)
    )
    for asset, match in zip(assets, analysis["machine_matches"]):
        if match.get("matched_id"):
            connection.execute("UPDATE intake_proposal_assets SET matched_machine_id=? WHERE id=?", (match["matched_id"], asset["id"]))
    ai_blockers = _ai_review_blockers(connection, proposal_id)
    analysis["blockers"] = list(dict.fromkeys([*(analysis.get("blockers") or []), *ai_blockers]))
    analysis["confirm_allowed"] = not analysis["blockers"]
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "OPERATOR", json.dumps({"smart_intake_2": analysis, "documents": source_documents, "resolutions": resolutions}, default=str), "Matching analysis refreshed after operator review."),
    )
    return analysis


def record_customer_resolution(
    connection: sqlite3.Connection, proposal_id: int, matched_customer_id: int | None,
) -> None:
    resolution = _latest_resolutions(connection, proposal_id)
    resolution["customer"] = "MATCHED" if matched_customer_id else "NEW"
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "OPERATOR", json.dumps({"resolutions": resolution}), "Operator resolved the customer match."),
    )


def _latest_resolutions(connection: sqlite3.Connection, proposal_id: int) -> dict:
    for row in connection.execute(
        "SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? ORDER BY id DESC",
        (proposal_id,),
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("resolutions"):
            return dict(payload["resolutions"])
    return {}


def record_machine_resolution(
    connection: sqlite3.Connection,
    proposal_id: int,
    proposal_asset_id: int,
    resolution: int | str,
) -> None:
    resolutions = _latest_resolutions(connection, proposal_id)
    machines = dict(resolutions.get("machines") or {})
    machines[str(proposal_asset_id)] = resolution
    resolutions["machines"] = machines
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "OPERATOR", json.dumps({"resolutions": resolutions}),
         "Operator resolved an ambiguous machine match."),
    )


def _prepare_proposal(
    raw_input: str,
    extracted_documents: list[dict],
    structured_candidates: dict | None = None,
) -> tuple[dict, str, dict]:
    extracted_documents = extracted_documents or []
    extracted_text = "\n\n".join(item.get("text", "") for item in extracted_documents if item.get("text"))
    parse_source = "\n\n".join(value for value in (raw_input, extracted_text) if value).strip()
    filenames = " ".join(item.get("filename", "") for item in extracted_documents)
    classification = classify_document(parse_source, filenames)
    supplier_invoice = supplier_invoice_fields(parse_source) if classification["document_type"] == "SUPPLIER_INVOICE" else None
    if supplier_invoice:
        if supplier_invoice.get("supplier_invoice_number"):
            classification["document_number"] = supplier_invoice["supplier_invoice_number"]
        parsed = parse_intake("")
        parsed.update({
            "raw_input": parse_source,
            "contact_name": supplier_invoice.get("customer_person") or "",
            "company_name": supplier_invoice.get("customer_company") or "",
            "location": supplier_invoice.get("customer_address") or "",
            "phone": "", "email": "", "assets": [], "unassigned_needs": [],
            "review_state": "REVIEW",
        })
    else:
        parsed = parse_intake(parse_source)
    candidates = structured_candidates or {}
    customer = candidates.get("customer")
    if customer is not None:
        parsed["contact_name"] = str(customer.get("name") or "").strip()
        parsed["company_name"] = str(customer.get("company") or "").strip()
        parsed["review_state"] = "REVIEW"
    candidate_machines = candidates.get("machines") or []
    asset_references: dict[str, int] = {}
    if candidate_machines:
        parsed["assets"] = []
        for index, candidate in enumerate(candidate_machines):
            reference = str(candidate.get("reference") or "").strip()
            manufacturer = str(candidate.get("manufacturer") or "").strip()
            asset_type = str(candidate.get("asset_type") or "other").strip().lower()
            identifiers = []
            for identifier in candidate.get("identifiers") or []:
                value = normalize_identifier(str(identifier.get("value") or ""))
                supplied_type = str(identifier.get("type") or "UNKNOWN")
                kind = classify_identifier(
                    value,
                    label=supplied_type.replace("_", " "),
                    manufacturer=manufacturer,
                    asset_category=asset_type,
                )
                identifiers.append({
                    "identifier_type": kind,
                    "value": value,
                    "component_label": str(identifier.get("component_label") or "").strip(),
                    "primary": bool(identifier.get("primary")),
                    "review_state": "REVIEW",
                })
            if identifiers and not any(item["primary"] for item in identifiers):
                identifiers[0]["primary"] = True
            asset_references[reference] = index
            parsed["assets"].append({
                "manufacturer": manufacturer,
                "model": str(candidate.get("model") or "").strip(),
                "year": str(candidate.get("year") or "").strip(),
                "asset_category": asset_type,
                "market_region": "UNKNOWN",
                "model_code": "",
                "review_state": "REVIEW",
                "identifiers": identifiers,
                "needs": [],
            })
    candidate_needs = candidates.get("requested_needs") or []
    if candidate_needs:
        for asset in parsed.get("assets") or []:
            asset["needs"] = []
        parsed["unassigned_needs"] = []
        for need in candidate_needs:
            wording = str(need.get("original_wording") or "")
            item = {
                "wording": wording,
                "original_wording": wording,
                "quantity": need.get("quantity"),
                "position": str(need.get("reference") or ""),
                "review_state": "REVIEW",
            }
            target = asset_references.get(str(need.get("machine_reference") or "").strip())
            if target is None or target >= len(parsed.get("assets") or []):
                item["review_state"] = "UNASSIGNED"
                parsed["unassigned_needs"].append(item)
            else:
                parsed["assets"][target]["needs"].append(item)
    return parsed, parse_source, classification


def _ai_submission(connection: sqlite3.Connection, proposal_id: int) -> dict | None:
    for row in connection.execute(
        "SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? AND contributor_type='AI' ORDER BY id DESC",
        (proposal_id,),
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("origin") in {"CHATGPT_MCP", "CHATGPT_FIREFOX"}:
            return payload
    return None


def _ai_review_blockers(connection: sqlite3.Connection, proposal_id: int) -> list[str]:
    submission = _ai_submission(connection, proposal_id)
    if submission is None:
        return []
    source_label = "ChatGPT Firefox" if submission.get("origin") == "CHATGPT_FIREFOX" else "ChatGPT MCP"
    review_count = int(connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM intake_proposals WHERE id=? AND review_state='REVIEW') +
        (SELECT COUNT(*) FROM intake_proposal_assets WHERE proposal_id=? AND included=1 AND review_state='REVIEW') +
        (SELECT COUNT(*) FROM intake_proposal_identifiers WHERE proposal_id=? AND review_state='REVIEW') +
        (SELECT COUNT(*) FROM intake_proposal_needs WHERE proposal_id=? AND included=1 AND review_state='REVIEW')""",
        (proposal_id, proposal_id, proposal_id, proposal_id),
    ).fetchone()[0])
    blockers = []
    if review_count:
        blockers.append(f"{source_label} proposal has {review_count} item(s) requiring explicit operator review.")
    candidates = submission.get("structured_candidates") or {}
    known_asset_references = {
        str(asset.get("reference") or "").strip()
        for asset in (candidates.get("assets") or candidates.get("machines") or [])
        if str(asset.get("reference") or "").strip()
    }
    unresolved_associations = sum(
        bool(reference) and reference not in known_asset_references
        for need in candidates.get("requested_needs") or []
        if (reference := str(need.get("machine_reference") or "").strip())
    )
    if unresolved_associations:
        blockers.append(
            f"{source_label} proposal has {unresolved_associations} unassigned Requested Need association(s)."
        )
    return blockers


def _insert_proposal_records(
    connection: sqlite3.Connection,
    *,
    raw_input: str,
    parsed: dict,
    parse_source: str,
    classification: dict,
    extracted_documents: list[dict],
    ai_payload: dict | None = None,
    ai_evidence: str = "",
) -> int:
    analysis = _analysis(connection, parsed, parse_source, classification)
    cursor = connection.execute(
        """INSERT INTO intake_proposals
        (raw_input,contact_name,company_name,location,phone,email,review_state)
        VALUES (?,?,?,?,?,?,?)""",
        (raw_input if ai_payload is not None else str(raw_input or "").strip() or parse_source,
         parsed["contact_name"], parsed["company_name"],
         parsed["location"], parsed["phone"], parsed["email"], parsed["review_state"]),
    )
    proposal_id = int(cursor.lastrowid)
    customer_match = analysis["customer_match"]
    if customer_match.get("matched_id"):
        connection.execute("UPDATE intake_proposals SET matched_customer_id=? WHERE id=?", (customer_match["matched_id"], proposal_id))
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
        match = analysis["machine_matches"][sequence - 1] if sequence <= len(analysis["machine_matches"]) else None
        for identifier in asset["identifiers"]:
            connection.execute(
                """INSERT INTO intake_proposal_identifiers
                (proposal_id,proposal_asset_id,identifier_type,identifier_value,component_label,is_primary,review_state)
                VALUES (?,?,?,?,?,?,?)""",
                (proposal_id, asset_id, identifier["identifier_type"], identifier["value"],
                 identifier["component_label"], int(identifier["primary"]), identifier["review_state"]),
            )
        if match and match.get("matched_id"):
            connection.execute("UPDATE intake_proposal_assets SET matched_machine_id=? WHERE id=?", (match["matched_id"], asset_id))
        for need_sequence, need in enumerate(asset["needs"], 1):
            connection.execute(
                """INSERT INTO intake_proposal_needs
                (proposal_id,proposal_asset_id,sequence,wording,original_wording,quantity,position,review_state)
                VALUES (?,?,?,?,?,?,?,?)""",
                (proposal_id, asset_id, need_sequence, need["wording"], need["original_wording"], need.get("quantity"), need.get("position", ""), need["review_state"]),
            )
    for need_sequence, need in enumerate(parsed["unassigned_needs"], 1):
        connection.execute(
            """INSERT INTO intake_proposal_needs
            (proposal_id,proposal_asset_id,sequence,wording,original_wording,quantity,position,review_state)
            VALUES (?,NULL,?,?,?,?,?,?)""",
            (proposal_id, need_sequence, need["wording"], need["original_wording"], need.get("quantity"), need.get("position", ""), need["review_state"]),
        )
    if ai_payload is not None:
        connection.execute(
            "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,'AI',?,?)",
            (proposal_id, json.dumps(ai_payload, sort_keys=True), ai_evidence),
        )
        ai_blockers = _ai_review_blockers(connection, proposal_id)
        analysis["blockers"] = list(dict.fromkeys([*(analysis.get("blockers") or []), *ai_blockers]))
        analysis["confirm_allowed"] = not analysis["blockers"]
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "DETERMINISTIC", json.dumps(parsed, default=str), "Original text preserved on proposal"),
    )
    connection.execute(
        "INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
        (proposal_id, "DETERMINISTIC", json.dumps({"smart_intake_2": analysis, "documents": extracted_documents}, default=str), classification["rationale"]),
    )
    return proposal_id


def create_proposal(
    connection: sqlite3.Connection, raw_input: str, *,
    extracted_documents: list[dict] | None = None,
) -> int:
    """Create the existing browser Smart Intake proposal without changing its behavior."""
    documents = extracted_documents or []
    parsed, parse_source, classification = _prepare_proposal(raw_input, documents)
    connection.execute("BEGIN IMMEDIATE")
    try:
        proposal_id = _insert_proposal_records(
            connection, raw_input=raw_input, parsed=parsed, parse_source=parse_source,
            classification=classification, extracted_documents=documents,
        )
        connection.commit()
        return proposal_id
    except Exception:
        connection.rollback()
        raise


def submit_structured_intake(
    connection: sqlite3.Connection,
    *,
    origin: str,
    client_reference: str,
    input_digest: str,
    original_input: str,
    structured_candidates: dict,
    actor: str,
    evidence: str,
) -> tuple[int, bool]:
    """Create or safely replay one transport-neutral structured DRAFT proposal."""
    if origin not in {"CHATGPT_MCP", "CHATGPT_FIREFOX", "CHATGPT_MOBILE"}:
        raise HTTPException(status_code=400, detail="Unsupported structured intake origin.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in connection.execute(
            "SELECT proposal_id,payload_json FROM intake_proposal_contributions WHERE contributor_type='AI' ORDER BY id",
        ):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("origin") != origin or payload.get("client_reference") != client_reference:
                continue
            if payload.get("input_digest") != input_digest:
                raise HTTPException(
                    status_code=409,
                    detail="Client reference already exists with different proposal content.",
                )
            existing = connection.execute(
                "SELECT status FROM intake_proposals WHERE id=?", (row["proposal_id"],)
            ).fetchone()
            if existing is None:
                raise HTTPException(status_code=409, detail="Client reference history is unavailable.")
            if existing["status"] != "DRAFT":
                raise HTTPException(status_code=409, detail="The correlated proposal is no longer DRAFT.")
            connection.rollback()
            return int(row["proposal_id"]), True

        parsed, parse_source, classification = _prepare_proposal(
            original_input, [], structured_candidates,
        )
        payload = {
            "origin": origin,
            "client_reference": client_reference,
            "input_digest": input_digest,
            "structured_candidates": structured_candidates,
        }
        proposal_id = _insert_proposal_records(
            connection, raw_input=original_input, parsed=parsed, parse_source=parse_source,
            classification=classification, extracted_documents=[], ai_payload=payload,
            ai_evidence=evidence,
        )
        write_audit(
            connection,
            action="SMART_INTAKE_PROPOSED",
            entity_type="INTAKE_PROPOSAL",
            entity_id=proposal_id,
            summary=f"{origin.replace('_', ' ').title()} intake proposal submitted for operator review",
            metadata={"origin": origin},
            actor=actor,
            request_id=client_reference,
        )
        connection.commit()
        return proposal_id, False
    except Exception:
        connection.rollback()
        raise


def submit_research_import(
    connection: sqlite3.Connection,
    package: ResearchImportPackage,
    *,
    pdf: ValidatedImage,
    actor: str = "research-import",
) -> tuple[int, bool]:
    """Stage one validated PDF+sidecar package as an untrusted DRAFT proposal.

    This function deliberately does not resolve or mutate the target Job.
    """
    if package.source_pdf.filename != pdf.original_filename:
        raise HTTPException(status_code=400, detail="Research package PDF filename does not match the sidecar.")
    package_json = package.model_dump(mode="json")
    package_digest = hashlib.sha256(
        json.dumps(package_json, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in connection.execute(
            "SELECT proposal_id,payload_json FROM intake_proposal_contributions "
            "WHERE contributor_type='AI' ORDER BY id"
        ):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("origin") != "RESEARCH_IMPORT_PACKAGE" or payload.get("package_id") != package.package_id:
                continue
            if payload.get("package_digest") != package_digest or payload.get("pdf_sha256") != package.source_pdf.sha256.lower():
                raise HTTPException(status_code=409, detail="Research package ID already exists with different content.")
            status = connection.execute(
                "SELECT status FROM intake_proposals WHERE id=?", (row["proposal_id"],)
            ).fetchone()
            if status is None or status["status"] != "DRAFT":
                raise HTTPException(status_code=409, detail="The correlated research proposal is no longer DRAFT.")
            connection.rollback()
            return int(row["proposal_id"]), True

        candidates = {
            "customer": package_json["customer"],
            "machines": [package_json["machine"]],
            "requested_needs": package_json["requested_needs"],
        }
        machine = candidates["machines"][0]
        machine["identifiers"] = machine.get("identifiers") or []
        for need in candidates["requested_needs"]:
            need["original_wording"] = need["original_wording"]
        parsed, parse_source, classification = _prepare_proposal(
            f"Research Import Package {package.package_id}", [], candidates,
        )
        classification = {
            "document_type": "CUSTOMER_REQUEST",
            "document_number": "",
            "review_required": False,
            "rationale": "Validated Research Import Package staged for operator review.",
        }
        proposal_id = _insert_proposal_records(
            connection,
            raw_input=f"Research Import Package {package.package_id}",
            parsed=parsed,
            parse_source=parse_source,
            classification=classification,
            extracted_documents=[{
                "filename": pdf.original_filename,
                "media_type": pdf.media_type,
                "text": pdf.extracted_text,
                "extraction_status": pdf.extraction_status,
                "extraction_evidence": pdf.extraction_evidence,
                "page_count": pdf.page_count,
            }],
        )
        stored_paths = store_proposal_images(connection, proposal_id, [pdf])
        try:
            connection.execute(
                    "INSERT INTO intake_proposal_contributions "
                    "(proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
                (proposal_id, "AI", json.dumps({
                    "origin": "RESEARCH_IMPORT_PACKAGE",
                    "package_id": package.package_id,
                    "package_digest": package_digest,
                    "pdf_filename": pdf.original_filename,
                    "pdf_sha256": package.source_pdf.sha256.lower(),
                    "package": package_json,
                }, sort_keys=True), "Validated PDF+JSON Research Import Package; candidate data only."),
            )
            write_audit(
                connection,
                action="RESEARCH_IMPORT_PROPOSED",
                entity_type="INTAKE_PROPOSAL",
                entity_id=proposal_id,
                summary=f"Research Import Package {package.package_id} staged for operator review",
                metadata={"package_id": package.package_id, "pdf_sha256": package.source_pdf.sha256.lower()},
                actor=actor,
                request_id=package.package_id,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            for path in stored_paths:
                path.unlink(missing_ok=True)
            raise
        return proposal_id, False
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


async def validate_research_import_uploads(
    research_pdf: UploadFile | None,
    sidecar: UploadFile | None = None,
) -> tuple[ResearchImportPackage, ValidatedImage]:
    """Validate the canonical PDF+JSON transport or a single .ppsresearch package."""
    if research_pdf is None or not research_pdf.filename:
        raise HTTPException(status_code=400, detail="Research Import requires a PDF+JSON sidecar or .ppsresearch package.")
    if Path(research_pdf.filename).suffix.lower() == ".ppsresearch":
        if sidecar is not None:
            raise HTTPException(status_code=400, detail="A .ppsresearch package must be uploaded without a sidecar.")
        package_bytes = await research_pdf.read(24 * 1024 * 1024 + 1)
        await research_pdf.close()
        if len(package_bytes) > 24 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Research package is too large.")
        try:
            package_data, source_name, source_bytes = unpack_ppsresearch(package_bytes)
            package = ResearchImportPackage.model_validate(package_data)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Research package is invalid.") from None
        source = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        source.write(source_bytes)
        source.seek(0)
        pdf_upload = UploadFile(filename=source_name, file=source, headers={"content-type": "application/pdf"})
        files = await validate_attachments([pdf_upload])
        pdf = files[0]
        actual_sha256 = hashlib.sha256(pdf.data).hexdigest()
        if actual_sha256 != package.source_pdf.sha256.lower():
            raise HTTPException(status_code=400, detail="Research package PDF SHA-256 does not match the manifest.")
        if package.source_pdf.filename != source_name:
            raise HTTPException(status_code=400, detail="Research package PDF filename does not match source.pdf.")
        return package, pdf
    if sidecar is None or not sidecar.filename:
        raise HTTPException(status_code=400, detail="Research Import requires both a PDF and JSON sidecar.")
    if Path(sidecar.filename).suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="Research Import sidecar must be a JSON file.")
    sidecar_bytes = await sidecar.read(2 * 1024 * 1024 + 1)
    await sidecar.close()
    if len(sidecar_bytes) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Research Import sidecar is too large.")
    try:
        package = ResearchImportPackage.model_validate(json.loads(sidecar_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Research Import sidecar is invalid.") from None
    files = await validate_attachments([research_pdf])
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="Research Import requires exactly one PDF.")
    pdf = files[0]
    actual_sha256 = hashlib.sha256(pdf.data).hexdigest()
    if Path(pdf.original_filename).name != package.source_pdf.filename:
        raise HTTPException(status_code=400, detail="Research package PDF filename does not match the sidecar.")
    if actual_sha256 != package.source_pdf.sha256.lower():
        raise HTTPException(status_code=400, detail="Research package PDF SHA-256 does not match the sidecar.")
    return package, pdf


def create_chatgpt_intake_proposal(
    connection: sqlite3.Connection,
    *,
    client_reference: str,
    input_digest: str,
    original_input: str,
    structured_candidates: dict,
    actor: str = "mcp-development",
) -> tuple[int, bool]:
    """Compatibility wrapper for the existing MCP proposal transport."""
    return submit_structured_intake(
        connection,
        origin="CHATGPT_MCP",
        client_reference=client_reference,
        input_digest=input_digest,
        original_input=original_input,
        structured_candidates=structured_candidates,
        actor=actor,
        evidence="Submitted through the authenticated PPS MCP proposal scope.",
    )


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
    result["attachments"] = [dict(item) for item in connection.execute(
        "SELECT * FROM intake_proposal_attachments WHERE proposal_id=? ORDER BY id", (proposal_id,)
    )]
    result["document_analysis"] = {}
    result["mcp_submission"] = None
    result["firefox_submission"] = None
    result["research_import"] = None
    for contribution in connection.execute(
        "SELECT contributor_type,payload_json FROM intake_proposal_contributions WHERE proposal_id=? ORDER BY id DESC", (proposal_id,)
    ):
        try:
            payload = json.loads(contribution["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if contribution["contributor_type"] == "AI" and payload.get("origin") == "CHATGPT_MCP" and result["mcp_submission"] is None:
            result["mcp_submission"] = {
                "origin": "CHATGPT_MCP",
                "client_reference": payload.get("client_reference") or "",
                "structured_candidates": payload.get("structured_candidates") or {},
            }
        if contribution["contributor_type"] == "AI" and payload.get("origin") == "CHATGPT_FIREFOX" and result["firefox_submission"] is None:
            result["firefox_submission"] = {
                "origin": "CHATGPT_FIREFOX",
                "client_reference": payload.get("client_reference") or "",
                "structured_candidates": payload.get("structured_candidates") or {},
            }
        if contribution["contributor_type"] == "AI" and payload.get("origin") == "RESEARCH_IMPORT_PACKAGE" and result["research_import"] is None:
            package = payload.get("package") or {}
            target = package.get("target") or {}
            target_job = connection.execute(
                """SELECT j.id,j.job_number,j.customer,j.company,j.machine,j.pin_serial,
                          j.customer_id,j.machine_id,m.manufacturer AS machine_manufacturer,
                          m.model AS machine_model,m.vin_pin_serial AS machine_identifier,
                          m.name AS machine_name,m.registry_type
                   FROM jobs j LEFT JOIN machines m ON m.id=j.machine_id
                  WHERE UPPER(j.job_number)=UPPER(?)""",
                (target.get("job_number") or "",),
            ).fetchone() if target.get("mode") == "EXISTING_JOB" else None
            target_identity = dict(target_job) if target_job else None
            if target_identity:
                target_identity["identifiers"] = [dict(identifier) for identifier in connection.execute(
                    "SELECT identifier_type,identifier_value,component_label,is_primary FROM machine_identifiers WHERE machine_id=? ORDER BY is_primary DESC,id",
                    (target_identity.get("machine_id"),),
                )]
            result["research_import"] = {
                "package": package,
                "package_id": payload.get("package_id") or package.get("package_id") or "",
                "package_digest": payload.get("package_digest") or "",
                "pdf_filename": payload.get("pdf_filename") or package.get("source_pdf", {}).get("filename") or "",
                "pdf_sha256": payload.get("pdf_sha256") or package.get("source_pdf", {}).get("sha256") or "",
                "target_mode": target.get("mode") or "",
                "target_job_number": target.get("job_number") or "",
                "target_job": target_identity,
                "target_found": target_identity is not None,
            }
        if payload.get("smart_intake_2") and not result["document_analysis"]:
            result["document_analysis"] = payload["smart_intake_2"]
            result["source_documents"] = payload.get("documents") or []
    result["review_summary"] = _review_summary(result)
    return result


def _review_summary(proposal: dict) -> dict:
    customer_label = proposal.get("contact_name") or proposal.get("company_name") or "Customer details missing"
    customer_action = "REUSE" if proposal.get("matched_customer_id") else "CREATE"
    machines, review_count, missing, all_needs = [], 0, [], list(proposal.get("unassigned_needs") or [])
    if str(proposal.get("review_state") or "").upper() == "REVIEW":
        review_count += 1
    if not (proposal.get("contact_name") or proposal.get("company_name")):
        missing.append("Customer name or company")
    for asset in proposal.get("assets") or []:
        label = " ".join(value for value in (asset.get("manufacturer"), asset.get("model")) if value).strip() or asset.get("name") or "Unnamed machine / asset"
        machines.append({"action": "REUSE" if asset.get("matched_machine_id") else "CREATE", "label": label})
        if str(asset.get("review_state") or "").upper() == "REVIEW":
            review_count += 1
        if not (asset.get("manufacturer") or asset.get("model") or asset.get("name")):
            missing.append(f"Machine {asset.get('sequence') or len(machines)} identity")
        review_count += sum(str(value.get("review_state") or "").upper() == "REVIEW" for value in asset.get("identifiers") or [])
        for need in asset.get("needs") or []:
            all_needs.append(need)
            review_count += str(need.get("review_state") or "").upper() == "REVIEW"
    unassigned_count = len(proposal.get("unassigned_needs") or [])
    customer_confirmed = str(proposal.get("review_state") or "").upper() == "CONFIDENT"
    included_assets = list(proposal.get("assets") or [])
    machine_confirmed = not included_assets or all(
        str(asset.get("review_state") or "").upper() == "CONFIDENT"
        and all(
            str(identifier.get("review_state") or "").upper() == "CONFIDENT"
            for identifier in asset.get("identifiers") or []
        )
        for asset in included_assets
    )
    need_confirmed = bool(all_needs) and all(
        str(need.get("review_state") or "").upper() == "CONFIDENT"
        for need in all_needs
    )
    core_reviews = {
        "customer": "CONFIRMED" if customer_confirmed else "REVIEW",
        "machine": "CONFIRMED" if machine_confirmed else "REVIEW",
        "requested_need": "CONFIRMED" if need_confirmed else "REVIEW",
    }
    core_review_count = sum(value == "REVIEW" for value in core_reviews.values())
    return {"customer": {"action": customer_action, "label": customer_label}, "machines": machines,
            "need_count": len(all_needs), "assigned_need_count": len(all_needs) - unassigned_count,
            "unassigned_need_count": unassigned_count, "review_count": review_count, "missing": missing,
            "core_reviews": core_reviews, "core_review_count": core_review_count,
            "confirmation_action": f"{customer_action.title()} the customer, create {len(all_needs)} Requested Need{'s' if len(all_needs) != 1 else ''}{f', create or link {len(machines)} optional asset context record' if machines else ''}, and open the new Job."}


def _primary_identifier(connection: sqlite3.Connection, asset_id: int):
    return connection.execute(
        "SELECT * FROM intake_proposal_identifiers WHERE proposal_asset_id=? ORDER BY is_primary DESC,id LIMIT 1",
        (asset_id,),
    ).fetchone()


def _carry_research_evidence_to_job(
    connection: sqlite3.Connection,
    proposal_id: int,
    job_id: int,
    asset_map: dict[int, int],
    need_map: dict[int, int],
) -> int:
    rows = connection.execute(
        "SELECT payload_json FROM intake_proposal_contributions "
        "WHERE proposal_id=? AND contributor_type='AI' ORDER BY id DESC",
        (proposal_id,),
    ).fetchall()
    evidence = None
    research_package = None
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("origin") == "RESEARCH_IMPORT_PACKAGE":
            research_package = payload.get("package") or {}
            break
        candidates = payload.get("structured_candidates") or {}
        if candidates.get("research_evidence"):
            evidence = candidates["research_evidence"]
            break
    if research_package is not None:
        needs_by_ref = {str(item.get("reference") or ""): item for item in research_package.get("requested_needs") or []}
        evidence = {
            "options": [], "claims": [], "quoted_evidence": [], "source_urls": [],
            "package_id": research_package.get("package_id") or "",
            "pdf_filename": (research_package.get("source_pdf") or {}).get("filename") or "",
            "pdf_sha256": (research_package.get("source_pdf") or {}).get("sha256") or "",
            "estimated_inbound_freight": research_package.get("estimated_inbound_freight"),
        }
        for raw_option in research_package.get("research_options") or []:
            option = dict(raw_option)
            need = needs_by_ref.get(str(option.get("requested_need_reference") or ""), {})
            source_urls = list((option.get("source_evidence") or {}).get("source_urls") or [])
            if option.get("supplier_url"):
                source_urls.append(option["supplier_url"])
            option["source_urls"] = list(dict.fromkeys(source_urls))
            option["requested_need_original_wording"] = need.get("original_wording") or ""
            option["requested_need_quantity"] = need.get("quantity")
            evidence["options"].append({
                "source_url": option.get("supplier_url") or (source_urls[0] if source_urls else ""),
                "source_name": option.get("supplier") or "Research Import supplier",
                "product_description": option.get("description") or "Research Import option",
                "part_number": option.get("oem_part_number") or "",
                "supplier_part_number": (option.get("cross_reference_part_numbers") or [""])[0],
                "price": option.get("tax_inclusive_unit_cost"),
                "quantity": option.get("quantity") or need.get("quantity") or 1,
                "currency": option.get("currency") or "USD",
                "research_notes": option.get("notes") or "",
                "confidence": option.get("confidence") or "UNKNOWN",
                "verification_status": option.get("verification_status") or "UNVERIFIED",
                "preserved": option,
            })
    if not evidence:
        return 0

    basket = get_or_create_basket(connection, job_id)
    ensure_initial_revision(connection, job_id)
    first_asset_id = next(iter(asset_map.values()), None)
    first_need_id = next(iter(need_map.values()), None)
    need_ids_by_reference = {}
    if research_package is not None:
        proposal_needs = connection.execute(
            "SELECT id,position FROM intake_proposal_needs WHERE proposal_id=? AND included=1 ORDER BY sequence,id",
            (proposal_id,),
        ).fetchall()
        need_ids_by_reference = {
            str(row["position"]): need_map[int(row["id"])]
            for row in proposal_needs
            if row["position"] and int(row["id"]) in need_map
        }
    options = list(evidence.get("options") or [])
    if not options:
        options = [{
            "source_url": (evidence.get("source_urls") or [""])[0],
            "source_name": "ChatGPT research",
            "product_description": "Smart Intake research evidence",
            "part_number": "",
            "price": None,
            "currency": "USD",
            "research_notes": "Research/reference material submitted with Smart Intake.",
            "confidence": "UNKNOWN",
            "verification_status": "UNVERIFIED",
        }]
    confidence_values = {"UNKNOWN": None, "LOW": 0.25, "MEDIUM": 0.5, "HIGH": 0.75}
    for sequence, option in enumerate(options, 1):
        source_name = str(option.get("source_name") or "ChatGPT research").strip()
        source_url = str(option.get("source_url") or "").strip()
        currency = str(option.get("currency") or "USD").strip().upper()
        source_id = int(connection.execute(
            "INSERT INTO basket_sources "
            "(basket_id,source_key,source_name,source_url,trust_level,currency) VALUES (?,?,?,?,?,?)",
            (basket["id"], f"smart-intake-{proposal_id}-{sequence}", source_name,
             source_url, "NEEDS_REVIEW", currency),
        ).lastrowid)
        preserved = dict(option.get("preserved") or option)
        preserved["claims"] = list(evidence.get("claims") or [])
        preserved["quoted_evidence"] = list(evidence.get("quoted_evidence") or [])
        preserved["source_urls"] = list(evidence.get("source_urls") or [])
        verification = str(option.get("verification_status") or "UNVERIFIED").upper()
        confidence = str(option.get("confidence") or "UNKNOWN").upper()
        option_need_id = need_ids_by_reference.get(str((option.get("preserved") or {}).get("requested_need_reference") or ""), first_need_id)
        connection.execute(
            """INSERT INTO basket_items (
                 basket_id,source_id,job_asset_id,primary_requested_need_id,research_state,
                 requested_description,manufacturer_part_number,supplier_part_number,supplier_name,
                 source_type,quantity,supplier_unit_cost,selected,confidence,source_url,
                 verification_status,verification_note,research_evidence,research_notes
               ) VALUES (?,?,?,?,?,?,?,?,?,'AFTERMARKET',?, ?,0,?,?,?,?,?,?)""",
            (basket["id"], source_id, first_asset_id, option_need_id, "RESEARCH_RESULT",
             str(option.get("product_description") or "Smart Intake research evidence").strip(),
             str(option.get("part_number") or "").strip(), str(option.get("supplier_part_number") or option.get("part_number") or "").strip(),
             source_name, option.get("quantity") or 1, option.get("price"), confidence_values.get(confidence), source_url,
             verification, "Untrusted Smart Intake research; operator review required.",
             json.dumps(preserved, sort_keys=True), str(option.get("research_notes") or "").strip()),
        )
        item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        if option_need_id is not None:
            connection.execute(
                "INSERT OR IGNORE INTO basket_item_need_links "
                "(basket_item_id,requested_need_id,relationship) VALUES (?,?,'SATISFIES')",
                (item_id, option_need_id),
            )
    if research_package is not None and evidence.get("estimated_inbound_freight"):
        connection.execute(
            "INSERT INTO basket_activity (basket_id,activity_type,details) VALUES (?,?,?)",
            (basket["id"], "RESEARCH_IMPORT_ESTIMATED_FREIGHT", json.dumps({
                "package_id": evidence.get("package_id"),
                "pdf_filename": evidence.get("pdf_filename"),
                "pdf_sha256": evidence.get("pdf_sha256"),
                "estimated_inbound_freight": evidence["estimated_inbound_freight"],
                "status": "ESTIMATED",
            }, sort_keys=True)),
        )
    write_audit(
        connection, action="SMART_INTAKE_RESEARCH_CARRIED_FORWARD",
        entity_type="JOB", entity_id=job_id,
        summary=f"Preserved {len(options)} untrusted Smart Intake research option(s) for operator review.",
        metadata={"proposal_id": proposal_id, "option_count": len(options)},
        actor="smart-intake-confirmation",
    )
    return len(options)


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
    research_import = connection.execute(
        "SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? AND contributor_type='AI' "
        "AND payload_json LIKE '%\"origin\": \"RESEARCH_IMPORT_PACKAGE\"%' LIMIT 1",
        (proposal_id,),
    ).fetchone()
    if research_import:
        payload = json.loads(research_import[0] or "{}") if research_import[0] else {}
        if (payload.get("package") or {}).get("target", {}).get("mode") == "EXISTING_JOB":
            connection.rollback()
            raise HTTPException(status_code=409, detail="EXISTING_JOB Research Imports require a separate target-review update flow.")
    if int(proposal["lock_version"]) != int(lock_version):
        connection.rollback()
        raise HTTPException(status_code=409, detail="This proposal changed in another tab. Reload and review it again.")
    refresh_proposal_analysis(connection, proposal_id)
    analysis_row = connection.execute(
        "SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? ORDER BY id DESC", (proposal_id,)
    ).fetchone()
    if analysis_row:
        try:
            document_analysis = json.loads(analysis_row["payload_json"] or "{}").get("smart_intake_2") or {}
        except (TypeError, ValueError):
            document_analysis = {}
        if document_analysis and not document_analysis.get("confirm_allowed", False):
            connection.rollback()
            raise HTTPException(status_code=409, detail="Resolve the Smart Intake document or matching blockers before Confirm & Create.")
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
    need_map: dict[int, int] = {}
    for need in needs:
        need_id = int(connection.execute(
            "INSERT INTO requested_needs (job_id,job_asset_id,customer_request_id,wording) VALUES (?,?,?,?)",
            (job_id, asset_map.get(need["proposal_asset_id"]), request_id, need["wording"]),
        ).lastrowid)
        need_map[int(need["id"])] = need_id
    research_option_count = _carry_research_evidence_to_job(
        connection, proposal_id, job_id, asset_map, need_map,
    )
    connection.execute("UPDATE customer_requests SET job_id=?,status='COMPLETED' WHERE id=?", (job_id, request_id))
    copied_attachment_paths: list[Path] = []
    try:
        copied_attachment_paths = copy_proposal_images_to_request(connection, proposal_id, request_id)
        changed = connection.execute(
        """UPDATE intake_proposals SET status='CONFIRMED',created_request_id=?,created_job_id=?,
        confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,lock_version=lock_version+1
        WHERE id=? AND status='DRAFT' AND lock_version=?""",
        (request_id, job_id, proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            raise HTTPException(status_code=409, detail="Another confirmation already completed.")
        connection.execute(
            "INSERT INTO job_timeline (job_id,event_type,icon,message) VALUES (?,?,?,?)",
            (job_id, "SMART_INTAKE_CONFIRMED", "", f"Smart Intake proposal {proposal_id} confirmed with {len(assets)} assets, {len(needs)} requested needs, and {research_option_count} untrusted research options."),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        for path in copied_attachment_paths:
            path.unlink(missing_ok=True)
        raise
    return job_id
