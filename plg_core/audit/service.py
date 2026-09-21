from __future__ import annotations
import json

def write_audit(
    connection,
    *,
    action: str,
    entity_type: str,
    entity_id=None,
    summary: str = "",
    metadata: dict | None = None,
    actor: str = "system",
    request_id: str = "",
) -> None:
    connection.execute("""
        INSERT INTO audit_logs (
            actor, action, entity_type, entity_id,
            summary, metadata_json, request_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        actor.strip() or "system",
        action.strip().upper(),
        entity_type.strip().upper(),
        str(entity_id or ""),
        summary.strip(),
        json.dumps(metadata or {}, default=str, sort_keys=True),
        request_id.strip(),
    ))
