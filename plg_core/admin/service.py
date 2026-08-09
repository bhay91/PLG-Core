from contextlib import closing
from legacy_app import get_connection

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
                ) AS supplier_spend
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
            "supplier_spend": round(
                float(
                    financials["supplier_spend"] or 0
                ),
                2,
            ),
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
    }
