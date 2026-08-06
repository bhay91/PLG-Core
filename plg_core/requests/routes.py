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
            (f"PLG-R{request_id:05d}", request_id),
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
    identifier = _normalize_match(
        parsed.get("identifier", "")
    )

    if identifier:
        rows = connection.execute(
            """
            SELECT *
            FROM machines
            WHERE customer_id = ?
              AND active = 1
              AND TRIM(COALESCE(vin_pin_serial, '')) != ''
            ORDER BY id
            """,
            (customer_id,),
        ).fetchall()

        for row in rows:
            if (
                _normalize_match(row["vin_pin_serial"])
                == identifier
            ):
                return row

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
def analyze_smart_intake(
    request: Request,
    raw_text: str = Form(...),
):
    parsed = _parse_smart_intake(raw_text)

    with closing(get_connection()) as connection:
        customer_match = _find_existing_customer(
            connection,
            parsed,
        )

        location_match = None
        machine_match = None

        if customer_match is not None:
            location_match = _find_existing_location(
                connection,
                customer_match["id"],
                parsed.get("location", ""),
            )

            machine_match = _find_existing_machine(
                connection,
                customer_match["id"],
                parsed,
            )

    return templates.TemplateResponse(
        request=request,
        name="smart_intake.html",
        context={
            "active_page": "requests",
            "raw_text": raw_text,
            "parsed": parsed,
            "customer_match": customer_match,
            "location_match": location_match,
            "machine_match": machine_match,
            "registry_types": REGISTRY_TYPES,
        },
    )


@router.post("/smart-intake/create")
def create_from_smart_intake(
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
                    f"PLG-C{customer_id:05d}",
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
                    f"PLG-M{machine_id:05d}",
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
                f"PLG-R{request_id:05d}",
                request_id,
            ),
        )

        job_number = next_job_number(connection)

        cursor = connection.execute(
            """
            INSERT INTO jobs (
                job_number,
                created_date,
                customer_id,
                machine_id,
                customer,
                company,
                phone,
                email,
                address,
                manufacturer,
                machine,
                pin_serial,
                status,
                notes
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'REQUESTED',
                ?
            )
            """,
            (
                job_number,
                date.today().isoformat(),
                customer_id,
                machine_id,
                customer["name"],
                customer["company"] or "",
                parsed["phone"] or customer["phone"] or "",
                parsed["email"] or customer["email"] or "",
                parsed["location"] or customer["address"] or "",
                parsed["manufacturer"],
                parsed["model"],
                parsed["identifier"],
                (
                    f"Created from PLG-R{request_id:05d}"
                    + (
                        f"\n\n{parsed['request_text']}"
                        if parsed["request_text"]
                        else ""
                    )
                ),
            ),
        )

        job_id = cursor.lastrowid

        parts = [
            line.strip(" -•\t")
            for line in parsed["requested_parts"].splitlines()
            if line.strip(" -•\t")
        ]

        for part in parts:
            connection.execute(
                """
                INSERT INTO job_parts (
                    job_id,
                    requested_description,
                    quantity
                )
                VALUES (?, ?, 1)
                """,
                (job_id, part),
            )

        connection.execute(
            """
            UPDATE customer_requests
            SET job_id = ?,
                status = 'COMPLETED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (job_id, request_id),
        )

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
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
    status: str = Form("NEW"),
    attachments: list[UploadFile] = File(default=[]),
):
    status = (status or "NEW").strip().upper()
    if status not in ALLOWED_STATUSES:
        status = "NEW"

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
                status = ?,
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
                status,
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
