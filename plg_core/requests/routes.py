from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
import re
import shutil
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from legacy_app import BASE_DIR, get_connection, next_job_number, templates

router = APIRouter(prefix="/requests", tags=["customer-requests"])
UPLOAD_ROOT = BASE_DIR / "uploads" / "requests"
ALLOWED_STATUSES = {"NEW", "WAITING", "READY", "COMPLETED"}
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


def _customer_display_name(record) -> str:
    return (record["individual_name"] or record["company_name"] or "").strip()


def _registry_display_name(record) -> str:
    display = " ".join(
        part for part in ((record["manufacturer"] or "").strip(), (record["model"] or "").strip())
        if part
    ).strip()
    return display or (record["identifier"] or "").strip() or "Registry Item"


@router.get("", response_class=HTMLResponse)
def list_requests(request: Request, q: str = "", status: str = "ALL"):
    status = status.upper().strip()
    if status not in ALLOWED_STATUSES | {"ALL"}:
        status = "ALL"
    q = q.strip()
    where = []
    params: list[object] = []
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
    return templates.TemplateResponse(
        request=request,
        name="requests.html",
        context={
            "requests": rows,
            "q": q,
            "status": status,
            "active_page": "requests",
            "today": date.today().isoformat(),
        },
    )


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
    reminder_date: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    values = [request_text, individual_name, company_name, phone, email, location, reminder_date]
    request_text, individual_name, company_name, phone, email, location, reminder_date = [
        (value or "").strip() for value in values
    ]
    if not any((request_text, individual_name, company_name, phone, email, attachments)):
        raise HTTPException(status_code=400, detail="Add a message, attachment, or contact detail.")
    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO customer_requests (
                request_number, request_text, individual_name, company_name,
                phone, email, location, reminder_date, status
            ) VALUES ('', ?, ?, ?, ?, ?, ?, NULLIF(?, ''), 'NEW')
            """,
            (request_text, individual_name, company_name, phone, email, location, reminder_date),
        )
        request_id = cursor.lastrowid
        connection.execute(
            "UPDATE customer_requests SET request_number = ? WHERE id = ?",
            (f"PLG-R{request_id:05d}", request_id),
        )
        await _save_attachments(connection, request_id, attachments)
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


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
    return templates.TemplateResponse(
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
        },
    )


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
    reminder_date: str = Form(""),
    status: str = Form("NEW"),
    attachments: list[UploadFile] = File(default=[]),
):
    status = status.upper().strip()
    if status not in ALLOWED_STATUSES:
        status = "NEW"
    values = [request_text, individual_name, company_name, phone, email, location, reminder_date]
    request_text, individual_name, company_name, phone, email, location, reminder_date = [
        (value or "").strip() for value in values
    ]
    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)
        connection.execute(
            """
            UPDATE customer_requests
            SET request_text = ?, individual_name = ?, company_name = ?,
                phone = ?, email = ?, location = ?, reminder_date = NULLIF(?, ''),
                status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (request_text, individual_name, company_name, phone, email, location,
             reminder_date, status, request_id),
        )
        await _save_attachments(connection, request_id, attachments)
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


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
                (f"PLG-C{customer_id:05d}", customer_id),
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
            (f"PLG-M{machine_id:05d}", machine_id),
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
        if record["job_id"]:
            return RedirectResponse(url=f"/jobs/{record['job_id']}/basket", status_code=303)
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
        job_id = cursor.lastrowid
        parts = [line.strip(" -•\t") for line in (record["requested_parts"] or "").splitlines()]
        for part in (part for part in parts if part):
            connection.execute(
                "INSERT INTO job_parts (job_id, requested_description, quantity) VALUES (?, ?, 1)",
                (job_id, part),
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
    return RedirectResponse(url=f"/jobs/{job_id}/basket", status_code=303)


@router.post("/{request_id}/status")
def update_status(request_id: int, status: str = Form(...)):
    status = status.upper().strip()
    if status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status.")
    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)
        connection.execute(
            "UPDATE customer_requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, request_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/requests/{request_id}", status_code=303)


@router.post("/{request_id}/delete")
def delete_request(request_id: int):
    with closing(get_connection()) as connection:
        _get_request_or_404(connection, request_id)
        connection.execute("DELETE FROM customer_requests WHERE id = ?", (request_id,))
        connection.commit()
    request_dir = UPLOAD_ROOT / str(request_id)
    if request_dir.exists():
        shutil.rmtree(request_dir)
    return RedirectResponse(url="/requests", status_code=303)


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
