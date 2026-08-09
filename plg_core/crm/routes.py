from contextlib import closing
from fastapi import APIRouter, HTTPException
from legacy_app import get_connection

router = APIRouter(prefix="/api/v1/crm", tags=["alpha16-crm"])

def search_records(
    q: str,
    limit: int = 8,
):
    query = str(q or "").strip()
    limit = max(1, min(int(limit or 8), 25))

    if not query:
        return {
            "query": "",
            "items": [],
        }

    like = f"%{query}%"
    prefix = f"{query}%"

    with closing(get_connection()) as connection:
        results = []

        customers = connection.execute(
            """
            SELECT
                'CUSTOMER' AS record_type,
                c.id,
                c.customer_number AS record_number,
                c.name AS title,
                CASE
                    WHEN TRIM(COALESCE(c.company,'')) != ''
                    THEN c.company
                    ELSE COALESCE(c.phone,'')
                END AS subtitle,
                '/customers/' || c.id AS url
            FROM customers c
            WHERE
                c.customer_number LIKE ? COLLATE NOCASE
                OR c.name LIKE ? COLLATE NOCASE
                OR c.company LIKE ? COLLATE NOCASE
                OR c.phone LIKE ? COLLATE NOCASE
                OR c.email LIKE ? COLLATE NOCASE
                OR c.address LIKE ? COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN c.customer_number LIKE ? COLLATE NOCASE
                      OR c.name LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                c.active DESC,
                c.name COLLATE NOCASE
            LIMIT ?
            """,
            (
                like, like, like, like, like, like,
                prefix, prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in customers)

        machines = connection.execute(
            """
            SELECT
                'MACHINE' AS record_type,
                m.id,
                m.machine_number AS record_number,
                COALESCE(
                    NULLIF(TRIM(m.name),''),
                    NULLIF(
                        TRIM(
                            COALESCE(m.manufacturer,'')
                            || ' '
                            || COALESCE(m.model,'')
                        ),
                        ''
                    ),
                    m.vin_pin_serial,
                    'Machine'
                ) AS title,
                TRIM(
                    COALESCE(c.name,'')
                    || CASE
                        WHEN TRIM(
                            COALESCE(m.vin_pin_serial,'')
                        ) != ''
                        THEN ' · ' || m.vin_pin_serial
                        ELSE ''
                    END
                ) AS subtitle,
                '/machines/' || m.id AS url
            FROM machines m
            LEFT JOIN customers c
              ON c.id=m.customer_id
            WHERE
                m.machine_number LIKE ? COLLATE NOCASE
                OR m.name LIKE ? COLLATE NOCASE
                OR m.manufacturer LIKE ? COLLATE NOCASE
                OR m.model LIKE ? COLLATE NOCASE
                OR m.vin_pin_serial LIKE ? COLLATE NOCASE
                OR c.name LIKE ? COLLATE NOCASE
                OR c.company LIKE ? COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN m.machine_number LIKE ? COLLATE NOCASE
                      OR m.vin_pin_serial LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                m.active DESC,
                m.id DESC
            LIMIT ?
            """,
            (
                like, like, like, like,
                like, like, like,
                prefix, prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in machines)

        jobs = connection.execute(
            """
            SELECT
                'JOB' AS record_type,
                j.id,
                j.job_number AS record_number,
                COALESCE(
                    NULLIF(TRIM(j.customer),''),
                    NULLIF(TRIM(j.company),''),
                    'Job'
                ) AS title,
                TRIM(
                    COALESCE(j.manufacturer,'')
                    || ' '
                    || COALESCE(j.machine,'')
                    || CASE
                        WHEN TRIM(
                            COALESCE(j.pin_serial,'')
                        ) != ''
                        THEN ' · ' || j.pin_serial
                        ELSE ''
                    END
                ) AS subtitle,
                '/jobs/' || j.id || '/basket' AS url
            FROM jobs j
            WHERE
                j.job_number LIKE ? COLLATE NOCASE
                OR j.customer LIKE ? COLLATE NOCASE
                OR j.company LIKE ? COLLATE NOCASE
                OR j.phone LIKE ? COLLATE NOCASE
                OR j.email LIKE ? COLLATE NOCASE
                OR j.manufacturer LIKE ? COLLATE NOCASE
                OR j.machine LIKE ? COLLATE NOCASE
                OR j.pin_serial LIKE ? COLLATE NOCASE
                OR j.notes LIKE ? COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN j.job_number LIKE ? COLLATE NOCASE
                      OR j.pin_serial LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                j.id DESC
            LIMIT ?
            """,
            (
                like, like, like, like, like,
                like, like, like, like,
                prefix, prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in jobs)

        quotes = connection.execute(
            """
            SELECT
                'QUOTE' AS record_type,
                q.id,
                q.quote_number AS record_number,
                COALESCE(
                    NULLIF(TRIM(j.customer),''),
                    'Quote'
                ) AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || CASE
                        WHEN TRIM(
                            COALESCE(j.pin_serial,'')
                        ) != ''
                        THEN ' · ' || j.pin_serial
                        ELSE ''
                    END
                ) AS subtitle,
                '/quotes/' || q.id || '/documents' AS url
            FROM quotes q
            JOIN jobs j
              ON j.id=q.job_id
            WHERE
                q.quote_number LIKE ? COLLATE NOCASE
                OR j.job_number LIKE ? COLLATE NOCASE
                OR j.customer LIKE ? COLLATE NOCASE
                OR j.company LIKE ? COLLATE NOCASE
                OR j.pin_serial LIKE ? COLLATE NOCASE
                OR EXISTS (
                    SELECT 1
                    FROM quote_items qi
                    WHERE qi.quote_id=q.id
                      AND (
                            qi.description LIKE ? COLLATE NOCASE
                            OR qi.supplier_part_number
                               LIKE ? COLLATE NOCASE
                      )
                )
            ORDER BY
                CASE
                    WHEN q.quote_number LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                q.id DESC
            LIMIT ?
            """,
            (
                like, like, like, like,
                like, like, like,
                prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in quotes)

        invoices = connection.execute(
            """
            SELECT
                'INVOICE' AS record_type,
                i.id,
                i.invoice_number AS record_number,
                COALESCE(
                    NULLIF(TRIM(j.customer),''),
                    'Invoice'
                ) AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || ' · '
                    || COALESCE(i.status,'')
                ) AS subtitle,
                '/invoices/' || i.id || '/documents' AS url
            FROM invoices i
            JOIN jobs j
              ON j.id=i.job_id
            WHERE
                i.invoice_number LIKE ? COLLATE NOCASE
                OR j.job_number LIKE ? COLLATE NOCASE
                OR j.customer LIKE ? COLLATE NOCASE
                OR j.company LIKE ? COLLATE NOCASE
                OR j.pin_serial LIKE ? COLLATE NOCASE
                OR EXISTS (
                    SELECT 1
                    FROM invoice_items ii
                    WHERE ii.invoice_id=i.id
                      AND (
                            ii.description LIKE ? COLLATE NOCASE
                            OR ii.supplier_part_number
                               LIKE ? COLLATE NOCASE
                      )
                )
            ORDER BY
                CASE
                    WHEN i.invoice_number LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                i.id DESC
            LIMIT ?
            """,
            (
                like, like, like, like,
                like, like, like,
                prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in invoices)

        orders = connection.execute(
            """
            SELECT
                'PURCHASE ORDER' AS record_type,
                po.id,
                po.po_number AS record_number,
                po.supplier_name AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || CASE
                        WHEN TRIM(
                            COALESCE(j.customer,'')
                        ) != ''
                        THEN ' · ' || j.customer
                        ELSE ''
                    END
                ) AS subtitle,
                '/purchasing/orders/' || po.id AS url
            FROM supplier_orders po
            JOIN jobs j
              ON j.id=po.job_id
            WHERE
                po.po_number LIKE ? COLLATE NOCASE
                OR po.supplier_name LIKE ? COLLATE NOCASE
                OR j.job_number LIKE ? COLLATE NOCASE
                OR j.customer LIKE ? COLLATE NOCASE
                OR j.pin_serial LIKE ? COLLATE NOCASE
                OR EXISTS (
                    SELECT 1
                    FROM supplier_order_items oi
                    WHERE oi.order_id=po.id
                      AND (
                            oi.description LIKE ? COLLATE NOCASE
                            OR oi.supplier_part_number
                               LIKE ? COLLATE NOCASE
                      )
                )
            ORDER BY
                CASE
                    WHEN po.po_number LIKE ? COLLATE NOCASE
                    THEN 0
                    ELSE 1
                END,
                po.id DESC
            LIMIT ?
            """,
            (
                like, like, like, like,
                like, like, like,
                prefix,
                limit,
            ),
        ).fetchall()

        results.extend(dict(row) for row in orders)

    return {
        "query": query,
        "items": results,
    }


@router.get("/search")
def global_search(
    q: str = "",
    limit: int = 8,
):
    return search_records(
        q=q,
        limit=limit,
    )


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
        machines = connection.execute(
            "SELECT * FROM machines WHERE customer_id=? ORDER BY id DESC",
            (customer_id,),
        ).fetchall()

        jobs = connection.execute(
            "SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC",
            (customer_id,),
        ).fetchall()

        quotes = connection.execute(
            """
            SELECT q.*
            FROM quotes q
            JOIN jobs j
              ON j.id=q.job_id
            WHERE j.customer_id=?
            ORDER BY q.id DESC
            """,
            (customer_id,),
        ).fetchall()

        invoices = connection.execute(
            """
            SELECT i.*
            FROM invoices i
            JOIN jobs j
              ON j.id=i.job_id
            WHERE j.customer_id=?
            ORDER BY i.id DESC
            """,
            (customer_id,),
        ).fetchall()

    result = dict(customer)
    result["machines"] = [dict(row) for row in machines]
    result["jobs"] = [dict(row) for row in jobs]
    result["quotes"] = [dict(row) for row in quotes]
    result["invoices"] = [dict(row) for row in invoices]
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
        jobs = connection.execute(
            "SELECT * FROM jobs WHERE machine_id=? ORDER BY id DESC",
            (machine_id,),
        ).fetchall()

        quotes = connection.execute(
            """
            SELECT q.*
            FROM quotes q
            JOIN jobs j
              ON j.id=q.job_id
            WHERE j.machine_id=?
            ORDER BY q.id DESC
            """,
            (machine_id,),
        ).fetchall()

        invoices = connection.execute(
            """
            SELECT i.*
            FROM invoices i
            JOIN jobs j
              ON j.id=i.job_id
            WHERE j.machine_id=?
            ORDER BY i.id DESC
            """,
            (machine_id,),
        ).fetchall()

    result = dict(machine)
    result["jobs"] = [dict(row) for row in jobs]
    result["quotes"] = [dict(row) for row in quotes]
    result["invoices"] = [dict(row) for row in invoices]
    return result
