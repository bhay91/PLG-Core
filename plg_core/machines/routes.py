from __future__ import annotations

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from legacy_app import get_connection, templates

router = APIRouter()

REGISTRY_TYPES = {
    "vehicle": {"label": "Vehicle", "icon": "🚗", "description": "Cars, SUVs, vans, pickups, and road vehicles.", "identifier_label": "VIN", "display_placeholder": "2024 Honda CR-V"},
    "machine": {"label": "Machine", "icon": "🚜", "description": "Heavy equipment, construction, agricultural, and industrial machines.", "identifier_label": "PIN / Serial", "display_placeholder": "CAT 420D Backhoe"},
    "engine": {"label": "Engine", "icon": "⚙", "description": "Standalone engines and engines identified primarily by model or serial.", "identifier_label": "Engine Serial", "display_placeholder": "Detroit Diesel 8V92"},
    "marine": {"label": "Marine", "icon": "🌊", "description": "Boats, outboards, inboards, marine engines, and related equipment.", "identifier_label": "Hull ID / Serial", "display_placeholder": "Yamaha 250 Outboard"},
    "generator": {"label": "Generator", "icon": "⚡", "description": "Portable, standby, and industrial generator sets.", "identifier_label": "Serial Number", "display_placeholder": "Cummins 100 kW Generator"},
    "trailer": {"label": "Trailer", "icon": "🚛", "description": "Road, utility, equipment, and commercial trailers.", "identifier_label": "VIN / Serial", "display_placeholder": "Great Dane Dry Van"},
    "component": {"label": "Component / Assembly", "icon": "🔧", "description": "Transmissions, axles, differentials, pumps, cylinders, ECUs, and major assemblies.", "identifier_label": "Part / Assembly Serial", "display_placeholder": "Allison 3000 Transmission"},
    "other": {"label": "Other", "icon": "📦", "description": "Anything that does not fit the standard categories.", "identifier_label": "VIN / PIN / Serial", "display_placeholder": "Customer Item"},
}


def _registry_type_context(registry_type: str):
    selected = REGISTRY_TYPES.get(registry_type, REGISTRY_TYPES["other"])
    return {
        "type_label": selected["label"],
        "type_description": selected["description"],
        "identifier_label": selected["identifier_label"],
        "display_placeholder": selected["display_placeholder"],
        "type_icon": selected["icon"],
    }



def _machine_form_context(*, title: str, subtitle: str, form_action: str,
                          cancel_url: str, machine, customers, registry_type: str = "other",
                          is_new: bool = False):
    return {
        "title": title,
        "subtitle": subtitle,
        "form_action": form_action,
        "cancel_url": cancel_url,
        "machine": machine,
        "customers": customers,
        "active_page": "machines",
        "registry_type": registry_type,
        "is_new": is_new,
        **_registry_type_context(registry_type),
    }


@router.get("/machines", response_class=HTMLResponse)
def list_machines(request: Request, view: str = "active"):
    if view not in {"active", "inactive", "all"}:
        view = "active"
    where = "" if view == "all" else ("WHERE machines.active = 1" if view == "active" else "WHERE machines.active = 0")
    with closing(get_connection()) as connection:
        machines = connection.execute(
            f"""
            SELECT machines.*, customers.name AS customer_name,
                   customers.company AS customer_company,
                   COUNT(DISTINCT jobs.id) AS jobs_count
            FROM machines
            JOIN customers ON customers.id = machines.customer_id
            LEFT JOIN jobs ON jobs.machine_id = machines.id
            {where}
            GROUP BY machines.id
            ORDER BY machines.updated_at DESC, machines.id DESC
            """
        ).fetchall()
    return templates.TemplateResponse(
        request=request, name="machines.html",
        context={
            "machines": machines, "view": view, "active_page": "machines",
            "registry_types": REGISTRY_TYPES,
        },
    )


@router.get("/machines/new", response_class=HTMLResponse)
def new_machine_form(
    request: Request,
    customer_id: int | None = None,
    registry_type: str | None = None,
):
    if registry_type not in REGISTRY_TYPES:
        return templates.TemplateResponse(
            request=request,
            name="registry_type_selector.html",
            context={
                "registry_types": [
                    {"value": key, **value} for key, value in REGISTRY_TYPES.items()
                ],
                "customer_id": customer_id,
                "active_page": "machines",
            },
        )
    with closing(get_connection()) as connection:
        customers = connection.execute(
            "SELECT * FROM customers WHERE active = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    machine = {
        "customer_id": customer_id or "", "name": "", "manufacturer": "",
        "model": "", "year": "", "vin_pin_serial": "", "engine": "",
        "engine_serial": "", "transmission": "", "component_details": "",
        "notes": "", "active": 1,
    }
    type_context = _registry_type_context(registry_type)
    return templates.TemplateResponse(
        request=request, name="machine_form.html",
        context=_machine_form_context(
            title=f"Register {type_context['type_label']}",
            subtitle="Create a reusable registry record.",
            form_action="/machines/new", cancel_url="/machines",
            machine=machine, customers=customers, registry_type=registry_type,
            is_new=True,
        ),
    )


@router.post("/machines/new")
def create_machine(
    customer_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    manufacturer: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
    vin_pin_serial: Annotated[str, Form()] = "",
    engine: Annotated[str, Form()] = "",
    engine_serial: Annotated[str, Form()] = "",
    transmission: Annotated[str, Form()] = "",
    component_details: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    registry_type: Annotated[str, Form()] = "other",
):
    values = [name, manufacturer, model, year, vin_pin_serial, engine,
              engine_serial, transmission, component_details, notes]
    name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes = [v.strip() for v in values]
    if not any((name, manufacturer, model, vin_pin_serial)):
        raise HTTPException(status_code=400, detail="Enter a name, manufacturer, model, or VIN/PIN/serial.")
    if not name:
        name = " ".join(part for part in (manufacturer, model) if part).strip() or vin_pin_serial
    with closing(get_connection()) as connection:
        customer = connection.execute(
            "SELECT id FROM customers WHERE id = ? AND active = 1", (customer_id,)
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=400, detail="Customer not found.")
        cursor = connection.execute(
            """
            INSERT INTO machines (customer_id, registry_type, name, manufacturer, model, year,
                vin_pin_serial, engine, engine_serial, transmission,
                component_details, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (customer_id, registry_type, name, manufacturer, model, year, vin_pin_serial, engine,
             engine_serial, transmission, component_details, notes),
        )
        machine_id = cursor.lastrowid
        connection.execute(
            "UPDATE machines SET machine_number = ? WHERE id = ?",
            (f"PPS-M-{machine_id:04d}", machine_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/machines/{machine_id}", status_code=303)


@router.get("/machines/{machine_id}", response_class=HTMLResponse)
def machine_detail(request: Request, machine_id: int):
    with closing(get_connection()) as connection:
        machine = connection.execute(
            """SELECT machines.*, customers.name AS customer_name,
                      customers.company AS customer_company,
                      customers.customer_number
               FROM machines JOIN customers ON customers.id = machines.customer_id
               WHERE machines.id = ?""", (machine_id,)
        ).fetchone()
        if machine is None:
            raise HTTPException(status_code=404, detail="Registry item not found.")
        jobs = connection.execute(
            "SELECT * FROM jobs WHERE machine_id = ? ORDER BY id DESC", (machine_id,)
        ).fetchall()
        quotes = connection.execute(
            """SELECT quotes.* FROM quotes JOIN jobs ON jobs.id = quotes.job_id
               WHERE jobs.machine_id = ? ORDER BY quotes.id DESC""", (machine_id,)
        ).fetchall()
        invoices = connection.execute(
            """SELECT invoices.* FROM invoices JOIN jobs ON jobs.id = invoices.job_id
               WHERE jobs.machine_id = ? ORDER BY invoices.id DESC""", (machine_id,)
        ).fetchall()
    return templates.TemplateResponse(
        request=request, name="machine_detail.html",
        context={
            "machine": machine, "jobs": jobs, "quotes": quotes,
            "invoices": invoices, "active_page": "machines",
            **_registry_type_context(machine["registry_type"] or "other"),
        },
    )


@router.get("/machines/{machine_id}/edit", response_class=HTMLResponse)
def edit_machine_form(request: Request, machine_id: int):
    with closing(get_connection()) as connection:
        machine = connection.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
        customers = connection.execute(
            "SELECT * FROM customers WHERE active = 1 OR id = ? ORDER BY name COLLATE NOCASE",
            (machine["customer_id"] if machine else -1,),
        ).fetchall()
    if machine is None:
        raise HTTPException(status_code=404, detail="Registry item not found.")
    return templates.TemplateResponse(
        request=request, name="machine_form.html",
        context=_machine_form_context(
            title="Edit Registry Item", subtitle=machine["machine_number"],
            form_action=f"/machines/{machine_id}/edit",
            cancel_url=f"/machines/{machine_id}", machine=machine, customers=customers,
            registry_type=machine["registry_type"] or "other",
        ),
    )


@router.post("/machines/{machine_id}/edit")
def update_machine(
    machine_id: int, customer_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "", manufacturer: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "", year: Annotated[str, Form()] = "",
    vin_pin_serial: Annotated[str, Form()] = "", engine: Annotated[str, Form()] = "",
    engine_serial: Annotated[str, Form()] = "", transmission: Annotated[str, Form()] = "",
    component_details: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
    registry_type: Annotated[str, Form()] = "other",
):
    values = [name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes]
    name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes = [v.strip() for v in values]
    if registry_type not in REGISTRY_TYPES:
        registry_type = "other"
    if not any((name, manufacturer, model, vin_pin_serial)):
        raise HTTPException(status_code=400, detail="Registry information is required.")
    if not name:
        name = " ".join(part for part in (manufacturer, model) if part).strip() or vin_pin_serial
    with closing(get_connection()) as connection:
        result = connection.execute(
            """UPDATE machines SET customer_id = ?, registry_type = ?, name = ?, manufacturer = ?,
               model = ?, year = ?, vin_pin_serial = ?, engine = ?,
               engine_serial = ?, transmission = ?, component_details = ?,
               notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (customer_id, registry_type, name, manufacturer, model, year, vin_pin_serial, engine,
             engine_serial, transmission, component_details, notes, machine_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registry item not found.")
        connection.execute(
            """UPDATE jobs SET customer_id = ?, manufacturer = ?, machine = ?,
               pin_serial = ? WHERE machine_id = ?""",
            (customer_id, manufacturer, model or name, vin_pin_serial, machine_id),
        )
        connection.commit()
    return RedirectResponse(url=f"/machines/{machine_id}", status_code=303)


@router.post("/machines/{machine_id}/deactivate")
def deactivate_machine(machine_id: int):
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE machines SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (machine_id,),
        )
        connection.commit()
    return RedirectResponse(url="/machines", status_code=303)


@router.post("/machines/{machine_id}/reactivate")
def reactivate_machine(machine_id: int):
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE machines SET active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (machine_id,),
        )
        connection.commit()
    return RedirectResponse(url=f"/machines/{machine_id}", status_code=303)
