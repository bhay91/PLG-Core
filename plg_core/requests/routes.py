from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
import json
import re
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from legacy_app import BASE_DIR, UPLOADS_DIR, get_connection, next_customer_number, next_job_number, next_machine_number, next_request_number, templates
from plg_core.machines.identifiers import find_machine_by_identifier
from plg_core.intake.service import create_proposal, load_proposal, submit_research_import, validate_research_import_uploads
from plg_core.intake.research_review import review_from_text
from plg_core.intake.attachments import store_proposal_images, validate_attachments

router = APIRouter(prefix="/requests", tags=["customer-requests"])
UPLOAD_ROOT = UPLOADS_DIR / "requests"
ALLOWED_STATUSES = {"NEW", "WAITING", "READY", "COMPLETED"}
MANUAL_STATUSES = ALLOWED_STATUSES - {"COMPLETED"}
REGISTRY_TYPES = {
    "vehicle": "Vehicle",
    "machine": "Machine",
    "engine": "Engine",
    "marine": "Marine",
    "generator": "Generator",
    "trailer": "Trailer",
    "component": "Component / Assembly",
    "other": "Other",
}


@router.get("/assistant/research", response_class=HTMLResponse)
def assistant_research_upload(request: Request):
    return RedirectResponse(url="/requests/smart-intake", status_code=303)


@router.post("/assistant/research/upload", response_class=HTMLResponse)
async def assistant_research_upload_submit(
    request: Request,
    research_pdf: UploadFile | None = File(default=None),
):
    """Create only a DRAFT review proposal; no authoritative records are touched."""
    if research_pdf is None or not research_pdf.filename:
        raise HTTPException(status_code=400, detail="Choose a research PDF.")
    files = await validate_attachments([research_pdf])
    pdf = files[0]
    raw = pdf.extracted_text or ""
    with closing(get_connection()) as connection:
        proposal_id = create_proposal(connection, raw or f"Research document: {pdf.original_filename}", extracted_documents=[{
            "filename": pdf.original_filename, "media_type": pdf.media_type, "text": pdf.extracted_text,
            "extraction_status": pdf.extraction_status, "extraction_evidence": pdf.extraction_evidence,
            "page_count": pdf.page_count,
        }])
        paths = store_proposal_images(connection, proposal_id, [pdf])
        review = review_from_text(raw, filename=pdf.original_filename)
        connection.execute("INSERT INTO intake_proposal_contributions (proposal_id,contributor_type,payload_json,evidence) VALUES (?,?,?,?)",
                           (proposal_id, "DETERMINISTIC", json.dumps({"origin": "PPS_ASSISTANT_RESEARCH", "review": review}, sort_keys=True), "Phase 1 provisional research extraction; no authoritative records created."))
        connection.commit()
    return RedirectResponse(url=f"/requests/assistant/research/review/{proposal_id}", status_code=303)


@router.get("/assistant/research/review/{proposal_id}", response_class=HTMLResponse)
def assistant_research_review(request: Request, proposal_id: int):
    with closing(get_connection()) as connection:
        proposal = load_proposal(connection, proposal_id)
        row = connection.execute("SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? AND payload_json LIKE '%PPS_ASSISTANT_RESEARCH%' ORDER BY id DESC LIMIT 1", (proposal_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Research review not found.")
    payload = json.loads(row["payload_json"] or "{}")
    return templates.TemplateResponse(request=request, name="research_review.html", context={"active_page": "requests", "proposal": proposal, "review": payload.get("review", {})})


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    return cleaned or "attachment"


def _get_request_or_404(connection, request_id: int):
    record = connection.execute(
        "SELECT * FROM customer_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if record is None:
        raise HTTPException(status_code=404, detail="Customer request not found.")
    return record


def _ensure_request_active(record) -> None:
    if int(record["is_cancelled"] or 0):
        raise HTTPException(
            status_code=409,
            detail="This request is cancelled. Its history is read-only.",
        )


def _customer_display_name(record) -> str:
    return (record["individual_name"] or record["company_name"] or "").strip()


def _registry_display_name(record) -> str:
    display = " ".join(
        part for part in ((record["manufacturer"] or "").strip(), (record["model"] or "").strip())
        if part
    ).strip()
    return display or (record["identifier"] or "").strip() or "Registry Item"


def _inbox_age(value: str, today: date) -> int | None:
    try:
        created = date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None
    return max((today - created).days, 0)


def _inbox_request_item(row, today: date) -> dict:
    item = dict(row)
    if int(item.get("is_archived") or 0):
        label, action = "Archived", "View History"
    elif int(item.get("is_cancelled") or 0):
        label, action = "Cancelled", "View History"
    elif item.get("job_id") and str(item.get("status") or "").upper() == "COMPLETED":
        label, action = "Job Created", "Open Job"
    elif str(item.get("status") or "").upper() == "WAITING":
        label, action = "Waiting for Information", "Open Request"
    elif str(item.get("status") or "").upper() == "READY":
        label, action = "Ready to Create Job", "Open Request"
    else:
        label, action = "New Request", "Review Request"
    machine = " ".join(filter(None, [item.get("manufacturer"), item.get("model")])).strip()
    removable = not any((
        int(item.get("is_archived") or 0), int(item.get("is_cancelled") or 0),
        item.get("job_id"), str(item.get("status") or "").upper() == "COMPLETED",
    ))
    return {
        "key": f"request:{item['id']}", "record_id": int(item["id"]), "entry_type": "Manual Request",
        "customer": item.get("company_name") or item.get("individual_name") or item.get("phone") or "Sender unknown",
        "preview": item.get("request_text") or item.get("requested_parts") or "Attachment only",
        "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
        "age_days": _inbox_age(item.get("created_at"), today),
        "review_label": label, "next_action": action,
        "url": f"/jobs/{item['job_id']}/basket?view=advanced" if action == "Open Job" else f"/requests/{item['id']}",
        "machine_need_summary": " · ".join(filter(None, [machine, item.get("requested_parts")])),
        "reminder_date": item.get("reminder_date") or "", "source": "REQUEST",
        "removable": removable,
        "remove_url": f"/requests/{item['id']}/remove-from-inbox" if removable else "",
        "remove_version": item.get("updated_at") or "",
        "delete_control_url": f"/requests/{item['id']}/delete-review",
    }


def _inbox_proposal_item(row, today: date) -> dict:
    item = dict(row)
    ready = str(item.get("review_state") or "").upper() == "CONFIDENT"
    machine_need = " · ".join(filter(None, [item.get("machine_summary"), item.get("need_summary")]))
    return {
        "key": f"proposal:{item['id']}", "record_id": int(item["id"]), "entry_type": "Smart Intake",
        "customer": item.get("company_name") or item.get("contact_name") or item.get("phone") or "Sender needs review",
        "preview": item.get("raw_input") or "Smart Intake proposal",
        "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
        "age_days": _inbox_age(item.get("created_at"), today),
        "review_label": "Ready for Review" if ready else "Needs Review",
        "next_action": "Review Intake",
        "url": f"/requests/smart-intake/proposals/{item['id']}",
        "machine_need_summary": machine_need, "reminder_date": "", "source": "PROPOSAL",
        "removable": str(item.get("status") or "").upper() == "DRAFT",
        "remove_url": f"/requests/smart-intake/proposals/{item['id']}/remove-from-inbox",
        "remove_version": item.get("lock_version"),
    }


@router.get("", response_class=HTMLResponse)
def list_requests(request: Request, q: str = "", status: str = "ALL", view: str = "active"):
    status = status.upper().strip()
    if status not in ALLOWED_STATUSES | {"ALL"}:
        status = "ALL"
    q = q.strip()
    where = []
    params: list[object] = []
    if view not in {"active", "archived", "all"}:
        view = "active"
    if view == "active":
        where.append("COALESCE(r.is_archived,0)=0")
    elif view == "archived":
        where.append("COALESCE(r.is_archived,0)=1")
    if status != "ALL":
        where.append("r.status = ?")
        params.append(status)
    if q:
        where.append(
            """(
                LOWER(COALESCE(r.request_text, '')) LIKE ? OR
                LOWER(COALESCE(r.individual_name, '')) LIKE ? OR
                LOWER(COALESCE(r.company_name, '')) LIKE ? OR
                LOWER(COALESCE(r.phone, '')) LIKE ? OR
                LOWER(COALESCE(r.email, '')) LIKE ? OR
                LOWER(COALESCE(r.manufacturer, '')) LIKE ? OR
                LOWER(COALESCE(r.model, '')) LIKE ? OR
                LOWER(COALESCE(r.identifier, '')) LIKE ?
            )"""
        )
        needle = f"%{q.lower()}%"
        params.extend([needle] * 8)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    report_date = date.today()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT r.*, COUNT(a.id) AS attachment_count
            FROM customer_requests r
            LEFT JOIN customer_request_attachments a ON a.request_id = r.id
            {where_sql}
            GROUP BY r.id
            ORDER BY
                CASE WHEN r.reminder_date IS NOT NULL AND r.reminder_date != '' THEN 0 ELSE 1 END,
                r.reminder_date ASC,
                r.updated_at DESC,
                r.id DESC
            """,
            params,
        ).fetchall()
        proposals = []
        if view != "archived" and status in {"ALL", "NEW"}:
            proposal_where = ["p.status='DRAFT'"]
            proposal_params: list[object] = []
            if q:
                proposal_where.append(
                    "(LOWER(p.raw_input) LIKE ? OR LOWER(p.contact_name) LIKE ? OR "
                    "LOWER(p.company_name) LIKE ? OR LOWER(p.phone) LIKE ? OR LOWER(p.email) LIKE ?)"
                )
                proposal_params.extend([f"%{q.lower()}%"] * 5)
            proposals = connection.execute(
                f"""
                SELECT p.*,
                       (SELECT GROUP_CONCAT(TRIM(a.manufacturer || ' ' || a.model), ', ')
                          FROM intake_proposal_assets a
                         WHERE a.proposal_id=p.id AND a.included=1) AS machine_summary,
                       (SELECT GROUP_CONCAT(n.wording, ', ')
                          FROM intake_proposal_needs n
                         WHERE n.proposal_id=p.id AND n.included=1) AS need_summary
                  FROM intake_proposals p
                 WHERE {' AND '.join(proposal_where)}
                 ORDER BY p.updated_at DESC,p.id DESC
                 LIMIT 500
                """,
                proposal_params,
            ).fetchall()
    inbox_items = [_inbox_request_item(row, report_date) for row in rows]
    inbox_items.extend(_inbox_proposal_item(row, report_date) for row in proposals)
    inbox_items.sort(
        key=lambda item: (
            0 if item["reminder_date"] else 1,
            item["reminder_date"] or "9999-12-31",
            str(item["updated_at"] or item["created_at"] or ""),
        ),
        reverse=False,
    )
    # Within ordinary (non-reminder) Inbox work, newest activity comes first.
    reminder_items = [item for item in inbox_items if item["reminder_date"]]
    ordinary_items = sorted(
        (item for item in inbox_items if not item["reminder_date"]),
        key=lambda item: str(item["updated_at"] or item["created_at"] or ""),
        reverse=True,
    )
    inbox_items = reminder_items + ordinary_items
    # A complete server-side preflight controls whether destructive review is offered.
    # The POST endpoint rebuilds the same plan under BEGIN IMMEDIATE before deleting.
    from plg_core.disposable.service import build_proposal_deletion_plan, build_request_deletion_plan
    with closing(get_connection()) as connection:
        for item in inbox_items:
            try:
                if item["source"] == "PROPOSAL":
                    plan = build_proposal_deletion_plan(item["record_id"], connection)
                    item["delete_disposable_url"] = f"/requests/smart-intake/proposals/{item['record_id']}/delete-disposable"
                else:
                    plan = build_request_deletion_plan(item["record_id"], connection)
                    item["delete_disposable_url"] = f"/requests/{item['record_id']}/delete-disposable"
                item["disposable_eligible"] = not plan["blockers"]
            except HTTPException:
                item["disposable_eligible"] = False
                item["delete_disposable_url"] = ""
    from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="requests.html",
        context={
            "requests": rows,
            "inbox_items": inbox_items,
            "q": q,
            "status": status,
            "view": view,
            "active_page": "requests",
            "today": report_date.isoformat(),
            "csrf_token": csrf_token,
        },
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token, httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


@router.get("/new", response_class=HTMLResponse)
def new_request_form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="request_form.html",
        context={
            "active_page": "requests",
            "title": "New Request",
            "form_action": "/requests/new",
            "cancel_url": "/requests",
            "record": {},
            "registry_types": REGISTRY_TYPES,
        },
    )


@router.post("/new")
async def create_request(
    request_text: str = Form(""),
    individual_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    location: str = Form(""),
    registry_type: str = Form("other"),
    manufacturer: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    identifier: str = Form(""),
    requested_parts: str = Form(""),
    reminder_date: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    registry_type = (registry_type or "other").strip().lower()
    if registry_type not in REGISTRY_TYPES:
        registry_type = "other"

    values = [
        request_text,
        individual_name,
        company_name,
        phone,
        email,
        location,
        manufacturer,
        model,
        year,
        identifier,
        requested_parts,
        reminder_date,
    ]

    (
        request_text,
        individual_name,
        company_name,
        phone,
        email,
        location,
        manufacturer,
        model,
        year,
        identifier,
        requested_parts,
        reminder_date,
    ) = [(value or "").strip() for value in values]

    if not any(
        (
            request_text,
            individual_name,
            company_name,
            phone,
            email,
            manufacturer,
            model,
            identifier,
            requested_parts,
            attachments,
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Add request, customer, machine, part, "
                "or attachment information."
            ),
        )

    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO customer_requests (
                request_number,
                request_text,
                individual_name,
                company_name,
                phone,
                email,
                location,
                registry_type,
                manufacturer,
                model,
                year,
                identifier,
                requested_parts,
                reminder_date,
                status
            )
            VALUES (
                '',
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                NULLIF(?, ''),
                'NEW'
            )
            """,
            (
                request_text,
                individual_name,
                company_name,
                phone,
                email,
                location,
                registry_type,
                manufacturer,
                model,
                year,
                identifier,
                requested_parts,
                reminder_date,
            ),
        )

        request_id = cursor.lastrowid

        connection.execute(
            """
            UPDATE customer_requests
            SET request_number = ?
            WHERE id = ?
            """,
            (next_request_number(connection), request_id),
        )

        await _save_attachments(
            connection,
            request_id,
            attachments,
        )

        connection.commit()

    return RedirectResponse(
        url=f"/requests/{request_id}",
        status_code=303,
    )



def _normalize_match(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").strip().lower(),
    )


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _smart_intake_registry_type(value: str) -> str:
    normalized = str(value or "").strip().lower()

    aliases = {
        "heavy equipment": "machine",
        "equipment": "machine",
        "backhoe": "machine",
        "excavator": "machine",
        "loader": "machine",
        "car": "vehicle",
        "truck": "vehicle",
        "automobile": "vehicle",
        "outboard": "marine",
        "boat": "marine",
        "genset": "generator",
    }

    normalized = aliases.get(normalized, normalized)

    if normalized not in REGISTRY_TYPES:
        return "other"

    return normalized


def _parse_smart_intake(raw_text: str) -> dict[str, str]:
    raw_text = str(raw_text or "").strip()

    result = {
        "individual_name": "",
        "company_name": "",
        "phone": "",
        "email": "",
        "location": "",
        "registry_type": "other",
        "manufacturer": "",
        "model": "",
        "year": "",
        "identifier": "",
        "requested_parts": "",
        "request_text": "",
    }

    label_map = {
        "customer": "individual_name",
        "customer name": "individual_name",
        "individual": "individual_name",
        "individual name": "individual_name",
        "name": "individual_name",
        "company": "company_name",
        "company name": "company_name",
        "phone": "phone",
        "telephone": "phone",
        "mobile": "phone",
        "email": "email",
        "location": "location",
        "island": "location",
        "shipping location": "location",
        "shipping destination": "location",
        "registry type": "registry_type",
        "equipment type": "registry_type",
        "type": "registry_type",
        "manufacturer": "manufacturer",
        "make": "manufacturer",
        "model": "model",
        "year": "year",
        "identifier": "identifier",
        "vin": "identifier",
        "pin": "identifier",
        "serial": "identifier",
        "serial number": "identifier",
        "engine serial": "identifier",
        "esn": "identifier",
        "customer message": "request_text",
        "message": "request_text",
        "notes": "request_text",
    }

    parts_labels = {
        "requested parts",
        "parts requested",
        "parts needed",
        "need",
        "needs",
        "parts",
    }

    lines = raw_text.splitlines()
    parts: list[str] = []
    notes: list[str] = []
    capture_parts = False
    capture_notes = False

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        match = re.match(
            r"^([A-Za-z][A-Za-z /_-]*?)\s*:\s*(.*)$",
            line,
        )

        if match:
            label = re.sub(
                r"\s+",
                " ",
                match.group(1).strip().lower(),
            )
            value = match.group(2).strip()

            capture_parts = label in parts_labels
            capture_notes = label in {
                "customer message",
                "message",
                "notes",
            }

            if capture_parts:
                if value:
                    parts.append(value)
                continue

            field = label_map.get(label)

            if field:
                if field == "registry_type":
                    result[field] = _smart_intake_registry_type(
                        value
                    )
                elif field == "request_text":
                    if value:
                        notes.append(value)
                else:
                    result[field] = value

                continue

            capture_parts = False
            capture_notes = False

        if capture_parts:
            cleaned = line.strip(" -•\t")
            if cleaned:
                parts.append(cleaned)
            continue

        if capture_notes:
            notes.append(line)

    result["requested_parts"] = "\n".join(parts)
    result["request_text"] = "\n".join(notes)

    return result


def _find_existing_customer(connection, parsed: dict[str, str]):
    phone = _normalize_phone(parsed.get("phone", ""))
    email = parsed.get("email", "").strip().lower()
    name = parsed.get("individual_name", "").strip()
    company = parsed.get("company_name", "").strip()

    if phone:
        rows = connection.execute(
            """
            SELECT *
            FROM customers
            WHERE active = 1
              AND TRIM(COALESCE(phone, '')) != ''
            ORDER BY id
            """
        ).fetchall()

        for row in rows:
            if _normalize_phone(row["phone"]) == phone:
                return row

    if email:
        row = connection.execute(
            """
            SELECT *
            FROM customers
            WHERE active = 1
              AND LOWER(TRIM(COALESCE(email, ''))) = ?
            ORDER BY id
            LIMIT 1
            """,
            (email,),
        ).fetchone()

        if row is not None:
            return row

    if name or company:
        rows = connection.execute(
            """
            SELECT *
            FROM customers
            WHERE active = 1
            ORDER BY id
            """
        ).fetchall()

        wanted_name = _normalize_match(name)
        wanted_company = _normalize_match(company)

        for row in rows:
            row_name = _normalize_match(row["name"])
            row_company = _normalize_match(row["company"])

            name_matches = (
                bool(wanted_name)
                and row_name == wanted_name
            )

            company_matches = (
                bool(wanted_company)
                and row_company == wanted_company
            )

            if name_matches or company_matches:
                return row

    return None


def _find_existing_location(
    connection,
    customer_id: int,
    location: str,
):
    wanted = _normalize_match(location)

    if not wanted:
        return None

    rows = connection.execute(
        """
        SELECT *
        FROM customer_locations
        WHERE customer_id = ?
          AND active = 1
        ORDER BY id
        """,
        (customer_id,),
    ).fetchall()

    for row in rows:
        if (
            _normalize_match(row["location_name"]) == wanted
            or _normalize_match(row["address"]) == wanted
        ):
            return row

    return None


def _find_existing_machine(
    connection,
    customer_id: int,
    parsed: dict[str, str],
):
    identifier = parsed.get("identifier", "").strip()

    if identifier:
        machine = find_machine_by_identifier(
            connection,
            identifier,
        )
        if machine is not None:
            return machine

    manufacturer = _normalize_match(
        parsed.get("manufacturer", "")
    )
    model = _normalize_match(parsed.get("model", ""))
    year = _normalize_match(parsed.get("year", ""))

    if manufacturer and model:
        rows = connection.execute(
            """
            SELECT *
            FROM machines
            WHERE customer_id = ?
              AND active = 1
            ORDER BY id
            """,
            (customer_id,),
        ).fetchall()

        for row in rows:
            same_manufacturer = (
                _normalize_match(row["manufacturer"])
                == manufacturer
            )
            same_model = (
                _normalize_match(row["model"] or row["name"])
                == model
            )
            same_year = (
                not year
                or not _normalize_match(row["year"])
                or _normalize_match(row["year"]) == year
            )

            if same_manufacturer and same_model and same_year:
                return row

    return None


@router.get("/smart-intake", response_class=HTMLResponse)
def smart_intake_form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="smart_intake.html",
        context={
            "active_page": "requests",
            "raw_text": "",
            "parsed": None,
            "customer_match": None,
            "location_match": None,
            "machine_match": None,
            "registry_types": REGISTRY_TYPES,
        },
    )


@router.post("/smart-intake/analyze", response_class=HTMLResponse)
async def analyze_smart_intake(
    request: Request,
    raw_text: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    files = await validate_attachments(attachments)
    if not raw_text.strip() and not files:
        raise HTTPException(status_code=400, detail="Paste request text or attach a supported document before analyzing it.")
    extracted_documents = [
        {
            "filename": item.original_filename,
            "media_type": item.media_type,
            "text": item.extracted_text,
            "extraction_status": item.extraction_status,
            "extraction_evidence": item.extraction_evidence,
            "page_count": item.page_count,
        }
        for item in files if item.media_type == "application/pdf"
    ]
    created_paths: list[Path] = []
    with closing(get_connection()) as connection:
        proposal_id = create_proposal(
            connection, raw_text, extracted_documents=extracted_documents,
        )
        try:
            created_paths = store_proposal_images(connection, proposal_id, files)
            connection.commit()
        except Exception:
            connection.rollback()
            connection.execute("DELETE FROM intake_proposals WHERE id=?", (proposal_id,))
            connection.commit()
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
    return RedirectResponse(url=f"/requests/smart-intake/proposals/{proposal_id}", status_code=303)


@router.post("/smart-intake/research-import")
async def ingest_research_import(
    request: Request,
    research_pdf: UploadFile | None = File(default=None),
    sidecar: UploadFile | None = None,
):
    """Stage a validated PDF+JSON package as a Smart Intake DRAFT only."""
    if research_pdf is None or not research_pdf.filename or (sidecar is not None and not sidecar.filename):
        raise HTTPException(status_code=400, detail="Research Import requires a PDF+JSON sidecar or .ppsresearch package.")
    package, pdf = await validate_research_import_uploads(research_pdf, sidecar)
    with closing(get_connection()) as connection:
        proposal_id, duplicate = submit_research_import(connection, package, pdf=pdf)
    return RedirectResponse(
        url=f"/requests/smart-intake/proposals/{proposal_id}",
        status_code=303,
        headers={"X-PPS-Research-Import-Duplicate": "1" if duplicate else "0"},
    )


@router.post("/smart-intake/create")
def create_from_smart_intake(
    request: Request,
    raw_text: str = Form(""),
    individual_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    location: str = Form(""),
    registry_type: str = Form("other"),
    manufacturer: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    identifier: str = Form(""),
    requested_parts: str = Form(""),
    request_text: str = Form(""),
):
    # Compatibility endpoint: even an old/stale form must cross the proposal
    # review boundary; it may never create authoritative records directly.
    if raw_text.strip():
        with closing(get_connection()) as connection:
            proposal_id = create_proposal(connection, raw_text)
        return RedirectResponse(
            url=f"/requests/smart-intake/proposals/{proposal_id}",
            status_code=303,
        )
    raise HTTPException(
        status_code=400,
        detail="Paste the original customer request and analyze it before creating a Job.",
    )

    # Historical implementation retained temporarily below for source-level
    # compatibility reference; it is unreachable by design.
    parsed = {
        "individual_name": individual_name.strip(),
        "company_name": company_name.strip(),
        "phone": phone.strip(),
        "email": email.strip(),
        "location": location.strip(),
        "registry_type": _smart_intake_registry_type(
            registry_type
        ),
        "manufacturer": manufacturer.strip(),
        "model": model.strip(),
        "year": year.strip(),
        "identifier": identifier.strip(),
        "requested_parts": requested_parts.strip(),
        "request_text": request_text.strip(),
    }

    if not (
        parsed["individual_name"]
        or parsed["company_name"]
    ):
        raise HTTPException(
            status_code=400,
            detail="Customer name or company is required.",
        )

    with closing(get_connection()) as connection:
        customer = _find_existing_customer(
            connection,
            parsed,
        )

        if customer is None:
            display_name = (
                parsed["individual_name"]
                or parsed["company_name"]
            )

            cursor = connection.execute(
                """
                INSERT INTO customers (
                    name,
                    company,
                    phone,
                    email,
                    address
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    display_name,
                    parsed["company_name"],
                    parsed["phone"],
                    parsed["email"],
                    parsed["location"],
                ),
            )

            customer_id = cursor.lastrowid

            connection.execute(
                """
                UPDATE customers
                SET customer_number = ?
                WHERE id = ?
                """,
                (
                    next_customer_number(connection),
                    customer_id,
                ),
            )

            customer = connection.execute(
                "SELECT * FROM customers WHERE id = ?",
                (customer_id,),
            ).fetchone()
        else:
            customer_id = customer["id"]

        customer_location = _find_existing_location(
            connection,
            customer_id,
            parsed["location"],
        )

        if (
            customer_location is None
            and parsed["location"]
        ):
            cursor = connection.execute(
                """
                INSERT INTO customer_locations (
                    customer_id,
                    location_name,
                    address,
                    phone,
                    email
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    customer_id,
                    parsed["location"],
                    parsed["location"],
                    parsed["phone"],
                    parsed["email"],
                ),
            )

            customer_location_id = cursor.lastrowid
        elif customer_location is not None:
            customer_location_id = customer_location["id"]
        else:
            customer_location_id = None

        machine = _find_existing_machine(
            connection,
            customer_id,
            parsed,
        )

        if (
            machine is not None
            and machine["customer_id"] != customer_id
        ):
            return templates.TemplateResponse(
                request=request,
                name="smart_intake.html",
                context={
                    "active_page": "requests",
                    "raw_text": raw_text,
                    "parsed": parsed,
                    "customer_match": customer,
                    "location_match": customer_location,
                    "machine_match": machine,
                    "machine_ownership_conflict": True,
                    "registry_types": REGISTRY_TYPES,
                },
            )

        if machine is None:
            machine_name = " ".join(
                value
                for value in (
                    parsed["manufacturer"],
                    parsed["model"],
                )
                if value
            ).strip()

            if not machine_name:
                machine_name = (
                    parsed["identifier"]
                    or "Registry Item"
                )

            cursor = connection.execute(
                """
                INSERT INTO machines (
                    customer_id,
                    customer_location_id,
                    registry_type,
                    name,
                    manufacturer,
                    model,
                    year,
                    vin_pin_serial,
                    notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_id,
                    customer_location_id,
                    parsed["registry_type"],
                    machine_name,
                    parsed["manufacturer"],
                    parsed["model"],
                    parsed["year"],
                    parsed["identifier"],
                    "Created through Smart Intake",
                ),
            )

            machine_id = cursor.lastrowid

            connection.execute(
                """
                UPDATE machines
                SET machine_number = ?
                WHERE id = ?
                """,
                (
                    next_machine_number(connection),
                    machine_id,
                ),
            )
        else:
            machine_id = machine["id"]

            if (
                customer_location_id
                and not machine["customer_location_id"]
            ):
                connection.execute(
                    """
                    UPDATE machines
                    SET customer_location_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        customer_location_id,
                        machine_id,
                    ),
                )

        cursor = connection.execute(
            """
            INSERT INTO customer_requests (
                request_number,
                request_text,
                individual_name,
                company_name,
                phone,
                email,
                location,
                registry_type,
                manufacturer,
                model,
                year,
                identifier,
                requested_parts,
                customer_id,
                customer_location_id,
                machine_id,
                status
            )
            VALUES (
                '',
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                'NEW'
            )
            """,
            (
                parsed["request_text"] or raw_text.strip(),
                parsed["individual_name"],
                parsed["company_name"],
                parsed["phone"],
                parsed["email"],
                parsed["location"],
                parsed["registry_type"],
                parsed["manufacturer"],
                parsed["model"],
                parsed["year"],
                parsed["identifier"],
                parsed["requested_parts"],
                customer_id,
                customer_location_id,
                machine_id,
            ),
        )

        request_id = cursor.lastrowid

        connection.execute(
            """
            UPDATE customer_requests
            SET request_number = ?
            WHERE id = ?
            """,
            (
                next_request_number(connection),
                request_id,
            ),
        )

        connection.commit()

    return RedirectResponse(
        url=f"/requests/{request_id}",
        status_code=303,
    )


@router.get("/{request_id}", response_class=HTMLResponse)
def request_detail(request: Request, request_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        attachments = connection.execute(
            "SELECT * FROM customer_request_attachments WHERE request_id = ? ORDER BY id DESC",
            (request_id,),
        ).fetchall()
        customers = connection.execute(
            "SELECT * FROM customers WHERE active = 1 ORDER BY name COLLATE NOCASE, company COLLATE NOCASE"
        ).fetchall()
        machines = []
        if record["customer_id"]:
            machines = connection.execute(
                "SELECT * FROM machines WHERE customer_id = ? AND active = 1 ORDER BY name COLLATE NOCASE",
                (record["customer_id"],),
            ).fetchall()
        customer = None
        machine = None
        job = None
        if record["customer_id"]:
            customer = connection.execute(
                "SELECT * FROM customers WHERE id = ?", (record["customer_id"],)
            ).fetchone()
        if record["machine_id"]:
            machine = connection.execute(
                "SELECT * FROM machines WHERE id = ?", (record["machine_id"],)
            ).fetchone()
        if record["job_id"]:
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (record["job_id"],)
            ).fetchone()
        attachment_removable = not any((
            record["job_id"],
            str(record["status"] or "").upper() == "COMPLETED",
            int(record["is_archived"] or 0),
            int(record["is_cancelled"] or 0),
        ))
        from plg_core.requests.service import get_request_delete_eligibility
        delete_eligibility = get_request_delete_eligibility(request_id, connection=connection)
    from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="request_detail.html",
        context={
            "record": record,
            "attachments": attachments,
            "customers": customers,
            "machines": machines,
            "customer": customer,
            "machine": machine,
            "job": job,
            "registry_types": REGISTRY_TYPES,
            "active_page": "requests",
            "today": date.today().isoformat(),
            "attachment_removable": attachment_removable,
            "delete_eligibility": delete_eligibility,
            "csrf_token": csrf_token,
        },
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token, httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


@router.get("/{request_id}/edit", response_class=HTMLResponse)
def edit_request_form(request: Request, request_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
    return templates.TemplateResponse(
        request=request,
        name="request_form.html",
        context={
            "active_page": "requests",
            "title": "Edit Request",
            "form_action": f"/requests/{request_id}/edit",
            "cancel_url": f"/requests/{request_id}",
            "record": record,
            "registry_types": REGISTRY_TYPES,
        },
    )


@router.post("/{request_id}/edit")
async def update_request(
    request_id: int,
    request_text: str = Form(""),
    individual_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    location: str = Form(""),
    registry_type: str = Form("other"),
    manufacturer: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    identifier: str = Form(""),
    requested_parts: str = Form(""),
    reminder_date: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    registry_type = (registry_type or "other").strip().lower()
    if registry_type not in REGISTRY_TYPES:
        registry_type = "other"

    values = [
        request_text,
        individual_name,
        company_name,
        phone,
        email,
        location,
        manufacturer,
        model,
        year,
        identifier,
        requested_parts,
        reminder_date,
    ]

    (
        request_text,
        individual_name,
        company_name,
        phone,
        email,
        location,
        manufacturer,
        model,
        year,
        identifier,
        requested_parts,
        reminder_date,
    ) = [(value or "").strip() for value in values]

    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)

        connection.execute(
            """
            UPDATE customer_requests
            SET request_text = ?,
                individual_name = ?,
                company_name = ?,
                phone = ?,
                email = ?,
                location = ?,
                registry_type = ?,
                manufacturer = ?,
                model = ?,
                year = ?,
                identifier = ?,
                requested_parts = ?,
                reminder_date = NULLIF(?, ''),
                    updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                request_text,
                individual_name,
                company_name,
                phone,
                email,
                location,
                registry_type,
                manufacturer,
                model,
                year,
                identifier,
                requested_parts,
                reminder_date,
                    request_id,
            ),
        )

        await _save_attachments(
            connection,
            request_id,
            attachments,
        )

        connection.commit()

    return RedirectResponse(
        url=f"/requests/{request_id}",
        status_code=303,
    )


@router.post("/{request_id}/prepare")
def prepare_request(
    request_id: int,
    registry_type: str = Form("other"),
    manufacturer: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    identifier: str = Form(""),
    requested_parts: str = Form(""),
):
    registry_type = registry_type.strip().lower()
    if registry_type not in REGISTRY_TYPES:
        registry_type = "other"
    values = [manufacturer, model, year, identifier, requested_parts]
    manufacturer, model, year, identifier, requested_parts = [(v or "").strip() for v in values]
    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)
        connection.execute(
            """
            UPDATE customer_requests
            SET registry_type = ?, manufacturer = ?, model = ?, year = ?,
                identifier = ?, requested_parts = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (registry_type, manufacturer, model, year, identifier, requested_parts, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)


@router.post("/{request_id}/customer/create")
def create_customer_from_request(request_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        if record["customer_id"]:
            return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)
        name = _customer_display_name(record)
        if not name:
            raise HTTPException(status_code=400, detail="Add an individual or company name first.")

        existing = None
        if (record["phone"] or "").strip():
            existing = connection.execute(
                "SELECT * FROM customers WHERE active = 1 AND TRIM(phone) = TRIM(?) ORDER BY id LIMIT 1",
                (record["phone"],),
            ).fetchone()
        if existing is None and (record["email"] or "").strip():
            existing = connection.execute(
                "SELECT * FROM customers WHERE active = 1 AND LOWER(TRIM(email)) = LOWER(TRIM(?)) ORDER BY id LIMIT 1",
                (record["email"],),
            ).fetchone()
        if existing is None:
            existing = connection.execute(
                """
                SELECT * FROM customers
                WHERE active = 1
                  AND LOWER(TRIM(name)) = LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(company, ''))) = LOWER(TRIM(?))
                ORDER BY id LIMIT 1
                """,
                (name, record["company_name"] or ""),
            ).fetchone()

        if existing is not None:
            customer_id = existing["id"]
        else:
            cursor = connection.execute(
                "INSERT INTO customers (name, company, phone, email, address) VALUES (?, ?, ?, ?, ?)",
                (name, record["company_name"] or "", record["phone"] or "",
                 record["email"] or "", record["location"] or ""),
            )
            customer_id = cursor.lastrowid
            connection.execute(
                "UPDATE customers SET customer_number = ? WHERE id = ?",
                (next_customer_number(connection), customer_id),
            )
        connection.execute(
            "UPDATE customer_requests SET customer_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (customer_id, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)


@router.post("/{request_id}/customer/link")
def link_customer_to_request(request_id: int, customer_id: int = Form(...)):
    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)
        customer = connection.execute(
            "SELECT id FROM customers WHERE id = ? AND active = 1", (customer_id,)
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=400, detail="Customer not found.")
        connection.execute(
            "UPDATE customer_requests SET customer_id = ?, machine_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (customer_id, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)


@router.post("/{request_id}/registry/create")
def create_registry_from_request(request_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        if not record["customer_id"]:
            raise HTTPException(status_code=400, detail="Create or link a customer first.")
        if record["machine_id"]:
            return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)
        if not any(((record["manufacturer"] or "").strip(), (record["model"] or "").strip(), (record["identifier"] or "").strip())):
            raise HTTPException(status_code=400, detail="Add make, model, or VIN / serial first.")
        existing_machine = find_machine_by_identifier(
            connection,
            record["identifier"] or "",
        )

        if existing_machine is not None:
            if existing_machine["customer_id"] != record["customer_id"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"VIN/PIN/serial already belongs to "
                        f"{existing_machine['machine_number']} "
                        f"({existing_machine['customer_name']}). "
                        "Transfer the machine before linking it to this request."
                    ),
                )
            machine_id = existing_machine["id"]
        else:
            display_name = _registry_display_name(record)
            cursor = connection.execute(
                """
                INSERT INTO machines (
                    customer_id, registry_type, name, manufacturer, model, year, vin_pin_serial, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record["customer_id"], record["registry_type"] or "other", display_name,
                 record["manufacturer"] or "", record["model"] or "", record["year"] or "",
                 record["identifier"] or "", f"Created from {record['request_number']}"),
            )
            machine_id = cursor.lastrowid
            connection.execute(
                "UPDATE machines SET machine_number = ? WHERE id = ?",
                (next_machine_number(connection), machine_id),
            )
        connection.execute(
            "UPDATE customer_requests SET machine_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (machine_id, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)


@router.post("/{request_id}/registry/link")
def link_registry_to_request(request_id: int, machine_id: int = Form(...)):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        if not record["customer_id"]:
            raise HTTPException(status_code=400, detail="Link a customer first.")
        machine = connection.execute(
            "SELECT id FROM machines WHERE id = ? AND customer_id = ? AND active = 1",
            (machine_id, record["customer_id"]),
        ).fetchone()
        if machine is None:
            raise HTTPException(status_code=400, detail="Registry item not found for this customer.")
        connection.execute(
            "UPDATE customer_requests SET machine_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (machine_id, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}#next-steps", status_code=303)



@router.post("/{request_id}/job/create")
def create_job_from_request(request_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        _ensure_request_active(record)

        if record["job_id"]:
            return RedirectResponse(
                url=f"/jobs/{record['job_id']}/basket?view=advanced",
                status_code=303,
            )

        if not record["customer_id"]:
            raise HTTPException(status_code=400, detail="Create or link a customer first.")
        customer = connection.execute(
            "SELECT * FROM customers WHERE id = ? AND active = 1", (record["customer_id"],)
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=400, detail="Linked customer is unavailable.")
        machine = None
        if record["machine_id"]:
            machine = connection.execute(
                "SELECT * FROM machines WHERE id = ? AND customer_id = ? AND active = 1",
                (record["machine_id"], customer["id"]),
            ).fetchone()
        manufacturer = (machine["manufacturer"] if machine else record["manufacturer"]) or ""
        model = ((machine["model"] or machine["name"]) if machine else record["model"]) or ""
        identifier = (machine["vin_pin_serial"] if machine else record["identifier"]) or ""
        job_number = next_job_number(connection)
        note_parts = [f"Created from {record['request_number']}"]
        if (record["request_text"] or "").strip():
            note_parts.append((record["request_text"] or "").strip())
        cursor = connection.execute(
            """
            INSERT INTO jobs (
                job_number, created_date, customer_id, machine_id, customer, company,
                phone, email, address, manufacturer, machine, pin_serial, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?)
            """,
            (job_number, date.today().isoformat(), customer["id"], record["machine_id"],
             customer["name"], customer["company"] or "", customer["phone"] or "",
             customer["email"] or "", customer["address"] or "", manufacturer,
             model, identifier, "\n\n".join(note_parts)),
        )
        job_id = int(cursor.lastrowid)
        job_asset_id = None
        if record["machine_id"] or any((manufacturer.strip(), model.strip(), identifier.strip())):
            asset_cursor = connection.execute(
                """
                INSERT INTO job_assets (
                    job_id,machine_id,customer_id,asset_type,name,manufacturer,
                    model,year,vin_pin_serial,is_primary
                ) VALUES (?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    job_id, record["machine_id"], customer["id"],
                    (machine["registry_type"] if machine else "") or "",
                    (machine["name"] if machine else "") or
                    " ".join(value for value in (manufacturer.strip(), model.strip()) if value),
                    manufacturer.strip(), model.strip(),
                    (machine["year"] if machine else record["year"]) or "",
                    identifier.strip(),
                ),
            )
            job_asset_id = int(asset_cursor.lastrowid)
        parts = [line.strip(" -•\t") for line in (record["requested_parts"] or "").splitlines()]
        for part in (part for part in parts if part):
            connection.execute(
                "INSERT INTO requested_needs "
                "(job_id,job_asset_id,customer_request_id,wording) VALUES (?,?,?,?)",
                (job_id, job_asset_id, request_id, part),
            )
        connection.execute(
            """
            UPDATE customer_requests
            SET job_id = ?, status = 'COMPLETED', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (job_id, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/jobs/{job_id}/basket?view=advanced", status_code=303)


@router.post("/{request_id}/status")
def update_status(request_id: int, status: str = Form(...)):
    status = status.upper().strip()
    if status not in MANUAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Request status may only be New, Waiting, or Ready.",
        )
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        _ensure_request_active(record)
        connection.execute(
            "UPDATE customer_requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


@router.get("/{request_id}/delete-review", response_class=HTMLResponse)
def request_delete_review(request: Request, request_id: int):
    from plg_core.requests.service import get_request_delete_eligibility
    from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request
    eligibility = get_request_delete_eligibility(request_id)
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="request_delete_review.html",
        context={
            "active_page": "requests", "eligibility": eligibility,
            "record": eligibility["request"], "csrf_token": csrf_token,
        },
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token, httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/{request_id}/delete")
def delete_request_web(
    request: Request,
    request_id: int,
    reason: str = Form(...),
    confirmation: str = Form(...),
    csrf_token: str = Form(""),
):
    from plg_core.requests.service import delete_request_safely
    from plg_core.web_security import require_valid_csrf, request_actor, request_id as audit_request_id
    require_valid_csrf(request, csrf_token)
    delete_request_safely(
        request_id, reason, confirmation, actor=request_actor(request),
        audit_request_id=audit_request_id(request),
    )
    return RedirectResponse(url="/requests?deleted=1", status_code=303)


def delete_request(request_id: int, reason: str, confirmation: str):
    """Service-compatible entry point retained for lifecycle callers and tests."""
    from plg_core.requests.service import delete_request_safely
    return delete_request_safely(request_id, reason, confirmation)


@router.post("/{request_id}/cancel")
def cancel_request(request_id: int, reason: str = Form(...)):
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Cancellation reason is required.")
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        connection.execute(
            "UPDATE customer_requests SET is_cancelled=1,cancelled_at=CURRENT_TIMESTAMP,"
            "cancellation_reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (reason, request_id),
        )
        from plg_core.audit import write_audit
        write_audit(connection, action="REQUEST_CANCELLED", entity_type="REQUEST",
                    entity_id=request_id,
                    summary=f"Request {record['request_number']} cancelled. Reason: {reason}",
                    metadata={"reason": reason})
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


@router.post("/{request_id}/archive")
def archive_request(request_id: int):
    return _set_request_archived(request_id, True)


@router.post("/{request_id}/remove-from-inbox")
def remove_request_from_inbox(request_id: int, expected_updated_at: str = Form(...)):
    """Archive active intake work without deleting its history or lineage."""
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        if any((record["job_id"], int(record["is_archived"] or 0),
                int(record["is_cancelled"] or 0), record["status"] == "COMPLETED")):
            raise HTTPException(status_code=409, detail="This request is no longer removable from the Inbox.")
        changed = connection.execute(
            """UPDATE customer_requests SET is_archived=1,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND updated_at=? AND job_id IS NULL AND status!='COMPLETED'
                 AND COALESCE(is_archived,0)=0 AND COALESCE(is_cancelled,0)=0""",
            (request_id, expected_updated_at),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="This request changed. Reload the Inbox before removing it.")
        from plg_core.audit import write_audit
        write_audit(connection, action="REQUEST_ARCHIVED", entity_type="REQUEST",
                    entity_id=request_id,
                    summary=f"Request {record['request_number']} removed from Inbox and archived")
        connection.commit()
    return RedirectResponse(url="/requests", status_code=303)


@router.post("/{request_id}/restore")
def restore_request(request_id: int):
    return _set_request_archived(request_id, False)


def _set_request_archived(request_id: int, archived: bool):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        connection.execute(
            "UPDATE customer_requests SET is_archived=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(archived), request_id),
        )
        from plg_core.audit import write_audit
        action = "REQUEST_ARCHIVED" if archived else "REQUEST_RESTORED"
        write_audit(connection, action=action, entity_type="REQUEST", entity_id=request_id,
                    summary=f"Request {record['request_number']} {'archived' if archived else 'restored'}")
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


@router.get("/{request_id}/attachments/{attachment_id}")
def download_attachment(request_id: int, attachment_id: int):
    with closing(get_connection()) as connection:
        attachment = connection.execute(
            """SELECT * FROM customer_request_attachments
               WHERE id = ? AND request_id = ?""",
            (attachment_id, request_id),
        ).fetchone()
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    path = Path(attachment["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment file is missing.")
    return FileResponse(path, filename=attachment["original_filename"])


@router.post("/{request_id}/attachments/{attachment_id}/delete")
def delete_attachment(request_id: int, attachment_id: int):
    with closing(get_connection()) as connection:
        record = _get_request_or_404(connection, request_id)
        if any((record["job_id"], str(record["status"] or "").upper() == "COMPLETED",
                int(record["is_archived"] or 0), int(record["is_cancelled"] or 0))):
            raise HTTPException(
                status_code=409,
                detail="Historical Request attachments are preserved and cannot be deleted.",
            )
        attachment = connection.execute(
            """SELECT * FROM customer_request_attachments
               WHERE id = ? AND request_id = ?""",
            (attachment_id, request_id),
        ).fetchone()
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        connection.execute(
            "DELETE FROM customer_request_attachments WHERE id = ?", (attachment_id,)
        )
        connection.commit()
    path = Path(attachment["file_path"])
    if path.exists():
        path.unlink()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


async def _save_attachments(connection, request_id: int, attachments: list[UploadFile]) -> None:
    for upload in attachments:
        if not upload or not upload.filename:
            continue
        request_dir = UPLOAD_ROOT / str(request_id)
        request_dir.mkdir(parents=True, exist_ok=True)
        original = Path(upload.filename).name
        stored = f"{uuid.uuid4().hex}_{_safe_filename(original)}"
        destination = request_dir / stored
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
        connection.execute(
            """
            INSERT INTO customer_request_attachments (
                request_id, original_filename, stored_filename,
                file_path, media_type
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (request_id, original, stored, str(destination), upload.content_type or ""),
        )
        await upload.close()
