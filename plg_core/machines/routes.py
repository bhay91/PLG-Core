from __future__ import annotations

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from legacy_app import get_connection, templates

router = APIRouter()


def _machine_form_context(*, title: str, subtitle: str, form_action: str,
                          cancel_url: str, machine, customers):
    return {
        "title": title,
        "subtitle": subtitle,
        "form_action": form_action,
        "cancel_url": cancel_url,
        "machine": machine,
        "customers": customers,
        "active_page": "machines",
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
        context={"machines": machines, "view": view, "active_page": "machines"},
    )


@router.get("/machines/new", response_class=HTMLResponse)
def new_machine_form(request: Request, customer_id: int | None = None):
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
    return templates.TemplateResponse(
        request=request, name="machine_form.html",
        context=_machine_form_context(
            title="New Machine", subtitle="Create a reusable machine profile.",
            form_action="/machines/new", cancel_url="/machines",
            machine=machine, customers=customers,
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
):
    values = [name, manufacturer, model, year, vin_pin_serial, engine,
              engine_serial, transmission, component_details, notes]
    name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes = [v.strip() for v in values]
    if not any((name, manufacturer, model, vin_pin_serial)):
        raise HTTPException(status_code=400, detail="Enter a machine name, manufacturer, model, or VIN/PIN/serial.")
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
            INSERT INTO machines (customer_id, name, manufacturer, model, year,
                vin_pin_serial, engine, engine_serial, transmission,
                component_details, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (customer_id, name, manufacturer, model, year, vin_pin_serial, engine,
             engine_serial, transmission, component_details, notes),
        )
        machine_id = cursor.lastrowid
        connection.execute(
            "UPDATE machines SET machine_number = ? WHERE id = ?",
            (f"PLG-M{machine_id:05d}", machine_id),
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
            raise HTTPException(status_code=404, detail="Machine not found.")
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
        context={"machine": machine, "jobs": jobs, "quotes": quotes,
                 "invoices": invoices, "active_page": "machines"},
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
        raise HTTPException(status_code=404, detail="Machine not found.")
    return templates.TemplateResponse(
        request=request, name="machine_form.html",
        context=_machine_form_context(
            title="Edit Machine", subtitle=machine["machine_number"],
            form_action=f"/machines/{machine_id}/edit",
            cancel_url=f"/machines/{machine_id}", machine=machine, customers=customers,
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
):
    values = [name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes]
    name, manufacturer, model, year, vin_pin_serial, engine, engine_serial, transmission, component_details, notes = [v.strip() for v in values]
    if not any((name, manufacturer, model, vin_pin_serial)):
        raise HTTPException(status_code=400, detail="Machine information is required.")
    if not name:
        name = " ".join(part for part in (manufacturer, model) if part).strip() or vin_pin_serial
    with closing(get_connection()) as connection:
        result = connection.execute(
            """UPDATE machines SET customer_id = ?, name = ?, manufacturer = ?,
               model = ?, year = ?, vin_pin_serial = ?, engine = ?,
               engine_serial = ?, transmission = ?, component_details = ?,
               notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (customer_id, name, manufacturer, model, year, vin_pin_serial, engine,
             engine_serial, transmission, component_details, notes, machine_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Machine not found.")
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
