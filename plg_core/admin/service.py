from contextlib import closing
from legacy_app import get_connection


def derive_invoice_financial_state(
    *, customer_total, estimated_cost, placed_cost, confirmed_actual_cost,
    actual_component_count, confirmed_actual_component_count,
):
    """Derive the shared invoice cost/profit projection without persisting it."""
    revenue = float(customer_total or 0)
    estimated = float(estimated_cost or 0)
    placed = float(placed_cost or 0)
    actual = float(confirmed_actual_cost or 0)
    component_count = int(actual_component_count or 0)
    confirmed_count = int(confirmed_actual_component_count or 0)
    if confirmed_count == 0:
        state = "NOT_CONFIRMED"
    elif confirmed_count < component_count:
        state = "PARTIALLY_CONFIRMED"
    else:
        state = "CONFIRMED"
    expected_profit = revenue - estimated
    actual_profit = revenue - actual
    return {
        "customer_total": round(revenue, 2),
        "booked_supplier_cost": round(estimated, 2),
        "placed_supplier_cost": round(placed, 2),
        "actual_supplier_cost": round(actual, 2),
        "expected_profit": round(expected_profit, 2),
        "placed_cost_profit": round(revenue - placed, 2),
        "actual_profit": round(actual_profit, 2),
        "cost_variance": round(actual - estimated, 2),
        "profit_variance": round(actual_profit - expected_profit, 2),
        "actual_cost_state": state,
        "actual_confirmed_components": confirmed_count,
        "actual_total_components": component_count,
    }


def invoice_financial_state(connection, invoice_id: int):
    """Return the authoritative reconciliation projection for one invoice."""
    row = connection.execute(
        """
        SELECT i.customer_total,i.supplier_total,
          COALESCE((SELECT SUM(po.order_total) FROM supplier_orders po
            WHERE po.invoice_id=i.id AND UPPER(COALESCE(po.status,''))
              IN ('ORDERED','PARTIAL','RECEIVED')),0) AS placed_cost,
          COALESCE((SELECT SUM(oi.actual_unit_cost * oi.quantity_ordered)
            FROM supplier_order_items oi JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.invoice_id=i.id AND UPPER(COALESCE(po.status,''))
              IN ('ORDERED','PARTIAL','RECEIVED') AND oi.actual_unit_cost IS NOT NULL),0)
          + COALESCE((SELECT SUM(po.actual_shipping_total) FROM supplier_orders po
            WHERE po.invoice_id=i.id AND UPPER(COALESCE(po.status,''))
              IN ('ORDERED','PARTIAL','RECEIVED') AND po.actual_shipping_total IS NOT NULL),0)
            AS confirmed_actual_cost,
          COALESCE((SELECT COUNT(*) FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id WHERE po.invoice_id=i.id
              AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')),0)
          + COALESCE((SELECT COUNT(*) FROM supplier_orders po WHERE po.invoice_id=i.id
              AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')),0)
            AS component_count,
          COALESCE((SELECT COUNT(*) FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id WHERE po.invoice_id=i.id
              AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
              AND oi.actual_unit_cost IS NOT NULL),0)
          + COALESCE((SELECT COUNT(*) FROM supplier_orders po WHERE po.invoice_id=i.id
              AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
              AND po.actual_shipping_total IS NOT NULL),0) AS confirmed_component_count
        FROM invoices i WHERE i.id=?
        """,
        (invoice_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Invoice not found.")
    return derive_invoice_financial_state(
        customer_total=row["customer_total"], estimated_cost=row["supplier_total"],
        placed_cost=row["placed_cost"], confirmed_actual_cost=row["confirmed_actual_cost"],
        actual_component_count=row["component_count"],
        confirmed_actual_component_count=row["confirmed_component_count"],
    )

def dashboard_snapshot():
    with closing(get_connection()) as connection:
        counts = {
            "customers": connection.execute("SELECT COUNT(*) FROM customers WHERE active=1").fetchone()[0],
            "machines": connection.execute("SELECT COUNT(*) FROM machines WHERE active=1").fetchone()[0],
            "open_jobs": connection.execute("SELECT COUNT(*) FROM jobs WHERE UPPER(COALESCE(status,'')) NOT IN ('DELIVERED','COMPLETED','COMPLETE','CLOSED')").fetchone()[0],
            "active_quotes": connection.execute("SELECT COUNT(*) FROM quotes WHERE COALESCE(is_archived,0)=0").fetchone()[0],
            "open_invoices": connection.execute("SELECT COUNT(*) FROM invoices WHERE status IN ('UNPAID','PARTIAL')").fetchone()[0],
            "supplier_orders": connection.execute("SELECT COUNT(*) FROM supplier_orders WHERE status NOT IN ('RECEIVED','CANCELLED')").fetchone()[0],
            "ready_deliveries": connection.execute("SELECT COUNT(*) FROM deliveries WHERE status='READY'").fetchone()[0],
        }
        money = connection.execute("""
            SELECT COALESCE(SUM(customer_total),0) AS revenue,
                   COALESCE(SUM(profit_total),0) AS profit,
                   COALESCE(SUM(balance_due),0) AS receivables
            FROM invoices WHERE status!='VOID'
        """).fetchone()
        audit = connection.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 20").fetchall()
    return {
        "counts": {key: int(value or 0) for key, value in counts.items()},
        "financials": {
            "invoiced_revenue": round(float(money["revenue"] or 0), 2),
            "invoiced_profit": round(float(money["profit"] or 0), 2),
            "outstanding_receivables": round(float(money["receivables"] or 0), 2),
        },
        "recent_audit": [dict(row) for row in audit],
    }



def accounting_snapshot():
    with closing(get_connection()) as connection:
        financials = connection.execute(
            """
            SELECT
                (
                    SELECT COALESCE(
                        SUM(customer_total),
                        0
                    )
                    FROM invoices
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) != 'VOID'
                ) AS invoiced_revenue,

                (
                    SELECT COALESCE(
                        SUM(profit_total),
                        0
                    )
                    FROM invoices
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) != 'VOID'
                ) AS booked_profit,

                (
                    SELECT COALESCE(
                        SUM(balance_due),
                        0
                    )
                    FROM invoices
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) IN ('UNPAID','PARTIAL')
                ) AS receivables,

                (
                    SELECT COALESCE(
                        SUM(amount),
                        0
                    )
                    FROM customer_transactions
                    WHERE UPPER(
                        COALESCE(transaction_type,'')
                    ) IN (
                        'PAYMENT',
                        'PAYMENT_REVERSAL'
                    )
                ) AS net_customer_payments,

                (
                    SELECT COALESCE(SUM(tx.amount),0)
                    FROM customer_transactions tx
                    JOIN invoices i ON i.id=tx.invoice_id
                    WHERE UPPER(COALESCE(tx.transaction_type,''))
                          IN ('PAYMENT','PAYMENT_REVERSAL')
                      AND UPPER(COALESCE(i.status,'')) != 'VOID'
                ) AS invoice_collections,

                (
                    SELECT COALESCE(SUM(amount),0)
                    FROM customer_transactions
                    WHERE invoice_id IS NULL
                      AND UPPER(COALESCE(transaction_type,''))='PAYMENT'
                ) AS unallocated_customer_money,

                (
                    SELECT COALESCE(-SUM(amount),0)
                    FROM customer_transactions
                    WHERE UPPER(COALESCE(transaction_type,''))='REFUND'
                ) AS refunds,

                (
                    SELECT COALESCE(SUM(amount),0)
                    FROM customer_transactions
                    WHERE UPPER(COALESCE(transaction_type,''))='ADJUSTMENT'
                ) AS adjustments,

                (
                    SELECT COALESCE(
                        SUM(order_total),
                        0
                    )
                    FROM supplier_orders po
                    JOIN invoices i ON i.id=po.invoice_id
                    WHERE UPPER(COALESCE(i.status,'')) != 'VOID'
                      AND UPPER(COALESCE(po.status,'')) IN (
                        'ORDERED',
                        'PARTIAL',
                        'RECEIVED'
                    )
                ) AS supplier_spend
            """
        ).fetchone()

        reconciliation_rows = connection.execute(
            """
            SELECT
                i.id AS invoice_id,i.invoice_number,i.job_id,j.job_number,
                COALESCE(NULLIF(TRIM(j.customer),''),NULLIF(TRIM(j.company),''),'Customer') AS customer,
                i.status,i.customer_total,i.balance_due,i.supplier_total,
                i.profit_total AS booked_profit,
                COALESCE((
                    SELECT SUM(tx.amount) FROM customer_transactions tx
                    WHERE tx.invoice_id=i.id
                      AND UPPER(COALESCE(tx.transaction_type,'')) IN ('PAYMENT','PAYMENT_REVERSAL')
                ),0) AS paid,
                COALESCE((
                    SELECT SUM(po.order_total) FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                ),0) AS placed_supplier_cost,
                COALESCE((
                    SELECT SUM(oi.actual_unit_cost * oi.quantity_ordered)
                    FROM supplier_order_items oi
                    JOIN supplier_orders po ON po.id=oi.order_id
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                      AND oi.actual_unit_cost IS NOT NULL
                ),0) + COALESCE((
                    SELECT SUM(po.actual_shipping_total)
                    FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                      AND po.actual_shipping_total IS NOT NULL
                ),0) AS confirmed_actual_cost,
                COALESCE((
                    SELECT COUNT(*) FROM supplier_order_items oi
                    JOIN supplier_orders po ON po.id=oi.order_id
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                ),0) + COALESCE((
                    SELECT COUNT(*) FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                ),0) AS actual_component_count,
                COALESCE((
                    SELECT COUNT(*) FROM supplier_order_items oi
                    JOIN supplier_orders po ON po.id=oi.order_id
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                      AND oi.actual_unit_cost IS NOT NULL
                ),0) + COALESCE((
                    SELECT COUNT(*) FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                      AND po.actual_shipping_total IS NOT NULL
                ),0) AS confirmed_actual_component_count,
                COALESCE((
                    SELECT COUNT(DISTINCT CASE
                        WHEN TRIM(COALESCE(ii.supplier_name,''))='' THEN 'Unassigned Supplier'
                        ELSE TRIM(ii.supplier_name) END)
                    FROM invoice_items ii WHERE ii.invoice_id=i.id
                ),0) AS expected_supplier_count,
                COALESCE((
                    SELECT COUNT(DISTINCT CASE
                        WHEN TRIM(COALESCE(po.supplier_name,''))='' THEN 'Unassigned Supplier'
                        ELSE TRIM(po.supplier_name) END)
                    FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                ),0) AS placed_supplier_count,
                COALESCE((SELECT COUNT(*) FROM supplier_orders po WHERE po.invoice_id=i.id),0) AS total_order_count,
                COALESCE((
                    SELECT COUNT(*) FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
                ),0) AS placed_order_count,
                COALESCE((
                    SELECT COUNT(*) FROM supplier_orders po
                    WHERE po.invoice_id=i.id
                      AND UPPER(COALESCE(po.status,''))='RECEIVED'
                ),0) AS received_order_count
            FROM invoices i
            JOIN jobs j ON j.id=i.job_id
            WHERE UPPER(COALESCE(i.status,'')) != 'VOID'
            ORDER BY i.invoice_date DESC,i.id DESC
            """
        ).fetchall()

        order_link_rows = connection.execute(
            """
            SELECT po.invoice_id,po.id,po.po_number,po.supplier_name,po.status
            FROM supplier_orders po
            JOIN invoices i ON i.id=po.invoice_id
            WHERE UPPER(COALESCE(i.status,'')) != 'VOID'
            ORDER BY po.invoice_id,po.id
            """
        ).fetchall()

        reconciled = connection.execute(
            """
            SELECT
                COUNT(*) AS invoice_count,
                COALESCE(
                    SUM(customer_total),
                    0
                ) AS revenue,
                COALESCE(
                    SUM(booked_profit),
                    0
                ) AS booked_profit,
                COALESCE(
                    SUM(actual_supplier_cost),
                    0
                ) AS supplier_cost
            FROM (
                SELECT
                    i.id,
                    i.customer_total,
                    i.profit_total AS booked_profit,
                    (
                        SELECT COALESCE(
                            SUM(po.order_total),
                            0
                        )
                        FROM supplier_orders po
                        WHERE po.invoice_id=i.id
                          AND UPPER(
                                COALESCE(po.status,'')
                              ) IN (
                                'ORDERED',
                                'PARTIAL',
                                'RECEIVED'
                              )
                    ) AS actual_supplier_cost
                FROM invoices i
                WHERE UPPER(
                        COALESCE(i.status,'')
                      ) != 'VOID'
                  AND EXISTS (
                        SELECT 1
                        FROM supplier_orders po
                        WHERE po.invoice_id=i.id
                      )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM supplier_orders po
                        WHERE po.invoice_id=i.id
                          AND UPPER(
                                COALESCE(po.status,'')
                              ) NOT IN (
                                'ORDERED',
                                'PARTIAL',
                                'RECEIVED'
                              )
                      )
            )
            """
        ).fetchone()

        month = connection.execute(
            """
            SELECT
                (
                    SELECT COALESCE(
                        SUM(customer_total),
                        0
                    )
                    FROM invoices
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) != 'VOID'
                      AND DATE(invoice_date)
                          >= DATE(
                              'now',
                              'start of month'
                          )
                ) AS invoiced_revenue,

                (
                    SELECT COALESCE(
                        SUM(amount),
                        0
                    )
                    FROM customer_transactions
                    WHERE UPPER(
                        COALESCE(transaction_type,'')
                    ) IN (
                        'PAYMENT',
                        'PAYMENT_REVERSAL'
                    )
                      AND DATE(transaction_date)
                          >= DATE(
                              'now',
                              'start of month'
                          )
                ) AS net_customer_payments,

                (
                    SELECT COALESCE(
                        SUM(order_total),
                        0
                    )
                    FROM supplier_orders
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) IN (
                        'ORDERED',
                        'PARTIAL',
                        'RECEIVED'
                    )
                      AND DATE(
                            COALESCE(
                                ordered_at,
                                created_at
                            )
                          )
                          >= DATE(
                              'now',
                              'start of month'
                          )
                ) AS supplier_spend,

                (
                    SELECT COUNT(*)
                    FROM invoices
                    WHERE UPPER(
                        COALESCE(status,'')
                    ) != 'VOID'
                      AND DATE(invoice_date)
                          >= DATE(
                              'now',
                              'start of month'
                          )
                ) AS invoice_count
            """
        ).fetchone()

        transactions = connection.execute(
            """
            SELECT
                tx.*,
                c.customer_number,
                c.name AS customer_name,
                i.invoice_number
            FROM customer_transactions tx
            LEFT JOIN customers c
              ON c.id=tx.customer_id
            LEFT JOIN invoices i
              ON i.id=tx.invoice_id
            ORDER BY
                tx.transaction_date DESC,
                tx.id DESC
            LIMIT 50
            """
        ).fetchall()

        audit = connection.execute(
            """
            SELECT *
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

        invoice_status = connection.execute(
            """
            SELECT
                UPPER(
                    COALESCE(status,'UNKNOWN')
                ) AS status,
                COUNT(*) AS count,
                COALESCE(
                    SUM(customer_total),
                    0
                ) AS total
            FROM invoices
            GROUP BY UPPER(
                COALESCE(status,'UNKNOWN')
            )
            ORDER BY status
            """
        ).fetchall()

        po_status = connection.execute(
            """
            SELECT
                UPPER(
                    COALESCE(status,'UNKNOWN')
                ) AS status,
                COUNT(*) AS count,
                COALESCE(
                    SUM(order_total),
                    0
                ) AS total
            FROM supplier_orders
            GROUP BY UPPER(
                COALESCE(status,'UNKNOWN')
            )
            ORDER BY status
            """
        ).fetchall()

    orders_by_invoice = {}
    for order in order_link_rows:
        orders_by_invoice.setdefault(int(order["invoice_id"]), []).append(dict(order))

    invoice_reconciliation = []
    for source_row in reconciliation_rows:
        row = dict(source_row)
        expected = int(row["expected_supplier_count"] or 0)
        placed_suppliers = int(row["placed_supplier_count"] or 0)
        total_orders = int(row["total_order_count"] or 0)
        placed_orders = int(row["placed_order_count"] or 0)
        received_orders = int(row["received_order_count"] or 0)
        if placed_orders == 0:
            state = "UNRECONCILED"
        elif total_orders != placed_orders or (expected > 0 and placed_suppliers < expected):
            state = "PARTIALLY_COSTED"
        elif received_orders == placed_orders:
            state = "RECEIVED"
        else:
            state = "ORDER_COSTED"
        financial_state = derive_invoice_financial_state(
            customer_total=row["customer_total"], estimated_cost=row["supplier_total"],
            placed_cost=row["placed_supplier_cost"],
            confirmed_actual_cost=row["confirmed_actual_cost"],
            actual_component_count=row["actual_component_count"],
            confirmed_actual_component_count=row["confirmed_actual_component_count"],
        )
        row.update({
            "paid": round(float(row["paid"] or 0), 2),
            "balance_due": round(float(row["balance_due"] or 0), 2),
            "booked_profit": round(float(row["booked_profit"] or 0), 2),
            "reconciliation_state": state,
            "orders": orders_by_invoice.get(int(row["invoice_id"]), []),
            "invoice_url": f"/invoices/{row['invoice_id']}/documents",
            "job_url": f"/jobs/{row['job_id']}/basket?view=advanced",
        })
        row.update(financial_state)
        invoice_reconciliation.append(row)

    invoiced_revenue = float(
        financials["invoiced_revenue"] or 0
    )
    booked_profit = float(
        financials["booked_profit"] or 0
    )

    margin = (
        booked_profit / invoiced_revenue * 100
        if invoiced_revenue > 0
        else 0
    )

    reconciled_revenue = float(
        reconciled["revenue"] or 0
    )
    reconciled_booked_profit = float(
        reconciled["booked_profit"] or 0
    )
    actual_rows = [
        row for row in invoice_reconciliation
        if row["actual_cost_state"] != "NOT_CONFIRMED"
    ]
    fully_actual_rows = [
        row for row in invoice_reconciliation
        if row["actual_cost_state"] == "CONFIRMED"
    ]
    actual_supplier_cost = sum(row["actual_supplier_cost"] for row in actual_rows)
    actual_revenue = sum(row["customer_total"] for row in actual_rows)
    actual_gross_profit = sum(row["actual_profit"] for row in actual_rows)
    actual_margin = (
        actual_gross_profit
        / actual_revenue
        * 100
        if actual_revenue > 0
        else 0
    )
    actual_expected_profit = sum(row["expected_profit"] for row in actual_rows)
    profit_variance = actual_gross_profit - actual_expected_profit
    cost_variance = sum(row["cost_variance"] for row in actual_rows)
    placed_cost_total = sum(row["placed_supplier_cost"] for row in invoice_reconciliation)
    placed_cost_profit_total = sum(row["placed_cost_profit"] for row in invoice_reconciliation)

    return {
        "financials": {
            "invoiced_revenue": round(
                invoiced_revenue,
                2,
            ),
            "booked_profit": round(
                booked_profit,
                2,
            ),
            "actual_gross_profit": round(
                actual_gross_profit,
                2,
            ),
            "actual_gross_margin_percent": round(
                actual_margin,
                1,
            ),
            "profit_variance": round(
                profit_variance,
                2,
            ),
            "cost_variance": round(cost_variance, 2),
            "actual_confirmation_state": (
                "NOT_CONFIRMED" if not actual_rows else
                "CONFIRMED" if len(fully_actual_rows) == len(invoice_reconciliation)
                else "PARTIALLY_CONFIRMED"
            ),
            "actual_confirmed_invoice_count": len(fully_actual_rows),
            "reconciled_invoice_count": int(
                reconciled["invoice_count"] or 0
            ),
            "reconciled_revenue": round(
                reconciled_revenue,
                2,
            ),
            "reconciled_booked_profit": round(
                reconciled_booked_profit,
                2,
            ),
            "actual_supplier_cost": round(
                actual_supplier_cost,
                2,
            ),
            "gross_margin_percent": round(
                margin,
                1,
            ),
            "receivables": round(
                float(
                    financials["receivables"] or 0
                ),
                2,
            ),
            "net_customer_payments": round(
                float(
                    financials[
                        "net_customer_payments"
                    ] or 0
                ),
                2,
            ),
            "invoice_collections": round(float(financials["invoice_collections"] or 0), 2),
            "unallocated_customer_money": round(float(financials["unallocated_customer_money"] or 0), 2),
            "refunds": round(float(financials["refunds"] or 0), 2),
            "adjustments": round(float(financials["adjustments"] or 0), 2),
            "supplier_spend": round(
                float(
                    financials["supplier_spend"] or 0
                ),
                2,
            ),
            "placed_supplier_cost": round(placed_cost_total, 2),
            "placed_cost_profit": round(placed_cost_profit_total, 2),
        },
        "month": {
            "invoiced_revenue": round(
                float(
                    month["invoiced_revenue"] or 0
                ),
                2,
            ),
            "net_customer_payments": round(
                float(
                    month[
                        "net_customer_payments"
                    ] or 0
                ),
                2,
            ),
            "supplier_spend": round(
                float(
                    month["supplier_spend"] or 0
                ),
                2,
            ),
            "invoice_count": int(
                month["invoice_count"] or 0
            ),
        },
        "transactions": [
            dict(row)
            for row in transactions
        ],
        "audit": [
            dict(row)
            for row in audit
        ],
        "invoice_status": [
            dict(row)
            for row in invoice_status
        ],
        "po_status": [
            dict(row)
            for row in po_status
        ],
        "invoice_reconciliation": invoice_reconciliation,
    }
