from contextlib import closing
from fastapi import APIRouter
from legacy_app import get_connection
from plg_core.admin.service import accounting_snapshot, dashboard_snapshot

router = APIRouter(prefix="/api/v1/admin", tags=["alpha17-18-admin"])

@router.get("/dashboard")
def dashboard():
    return dashboard_snapshot()

@router.get("/accounting")
def accounting():
    return accounting_snapshot()


@router.get("/audit")
def audit(limit: int = 100, entity_type: str = "", entity_id: str = ""):
    limit = max(1, min(int(limit), 500))
    conditions = []
    params = []
    if entity_type.strip():
        conditions.append("entity_type=?")
        params.append(entity_type.strip().upper())
    if entity_id.strip():
        conditions.append("entity_id=?")
        params.append(entity_id.strip())
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"SELECT * FROM audit_logs {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}
