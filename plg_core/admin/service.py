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
