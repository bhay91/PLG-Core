from contextlib import closing
from fastapi import APIRouter, HTTPException
from legacy_app import get_connection

router = APIRouter(prefix="/api/v1/crm", tags=["alpha16-crm"])

@router.get("/customers")
def customers(limit: int = 100):
    limit = max(1, min(int(limit), 500))
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT c.*, COUNT(DISTINCT j.id) AS jobs_count,
                   COUNT(DISTINCT m.id) AS machines_count
            FROM customers c
            LEFT JOIN jobs j ON j.customer_id=c.id
            LEFT JOIN machines m ON m.customer_id=c.id
            WHERE c.active=1
            GROUP BY c.id ORDER BY c.name COLLATE NOCASE LIMIT ?
        """, (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}

@router.get("/customers/{customer_id}")
def customer_detail(customer_id: int):
    with closing(get_connection()) as connection:
        customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        machines = connection.execute("SELECT * FROM machines WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()
        jobs = connection.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()
    result = dict(customer)
    result["machines"] = [dict(row) for row in machines]
    result["jobs"] = [dict(row) for row in jobs]
    return result

@router.get("/machines")
def machines(limit: int = 100):
    limit = max(1, min(int(limit), 500))
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT m.*, c.customer_number, c.name AS customer_name,
                   COUNT(DISTINCT j.id) AS jobs_count
            FROM machines m
            JOIN customers c ON c.id=m.customer_id
            LEFT JOIN jobs j ON j.machine_id=m.id
            WHERE m.active=1
            GROUP BY m.id ORDER BY m.id DESC LIMIT ?
        """, (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}

@router.get("/machines/{machine_id}")
def machine_detail(machine_id: int):
    with closing(get_connection()) as connection:
        machine = connection.execute("""
            SELECT m.*, c.customer_number, c.name AS customer_name
            FROM machines m JOIN customers c ON c.id=m.customer_id
            WHERE m.id=?
        """, (machine_id,)).fetchone()
        if machine is None:
            raise HTTPException(status_code=404, detail="Machine not found.")
        jobs = connection.execute("SELECT * FROM jobs WHERE machine_id=? ORDER BY id DESC", (machine_id,)).fetchall()
    result = dict(machine)
    result["jobs"] = [dict(row) for row in jobs]
    return result
