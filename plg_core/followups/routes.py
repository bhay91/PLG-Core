from __future__ import annotations

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.timeline import log_job_event


router = APIRouter(tags=["follow-ups"])


def _required(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    return normalized


@router.post("/jobs/{job_id}/follow-ups")
def create_job_follow_up(
    job_id: int,
    summary: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "CUSTOMER_INFORMATION",
):
    summary = _required(summary, "Information or action needed")
    category = str(category or "CUSTOMER_INFORMATION").strip().upper()
    if category not in {"CUSTOMER_INFORMATION", "OPERATOR_ATTENTION"}:
        raise HTTPException(status_code=400, detail="Invalid follow-up category.")
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id,job_number FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        cursor = connection.execute(
            "INSERT INTO job_follow_ups(job_id,category,summary,reason) "
            "VALUES (?,?,?,?)",
            (job_id, category, summary, str(reason or "").strip()),
        )
        follow_up_id = int(cursor.lastrowid)
        action = (
            "CUSTOMER_INFORMATION_REQUESTED"
            if category == "CUSTOMER_INFORMATION"
            else "OPERATOR_ATTENTION_CREATED"
        )
        message = (
            f"Waiting for customer information: {summary}"
            if category == "CUSTOMER_INFORMATION"
            else f"Needs operator attention: {summary}"
        )
        write_audit(
            connection, action=action, entity_type="JOB_FOLLOW_UP",
            entity_id=follow_up_id, summary=message,
            metadata={"job_id": job_id, "reason": str(reason or "").strip()},
        )
        log_job_event(
            connection, job_id=job_id, event_type=action,
            icon="?" if category == "CUSTOMER_INFORMATION" else "!",
            message=message,
        )
        connection.commit()
    return RedirectResponse(url=f"/jobs/{job_id}/basket#follow-ups", status_code=303)


def _open_follow_up(connection, follow_up_id: int):
    row = connection.execute(
        "SELECT * FROM job_follow_ups WHERE id=?", (follow_up_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    return row


@router.post("/follow-ups/{follow_up_id}/information-received")
def information_received(
    follow_up_id: int,
    resolution: Annotated[str, Form()],
):
    resolution = _required(resolution, "Information received")
    with closing(get_connection()) as connection:
        follow_up = _open_follow_up(connection, follow_up_id)
        if follow_up["status"] == "RECEIVED":
            return RedirectResponse(
                url=f"/jobs/{follow_up['job_id']}/basket#follow-ups", status_code=303
            )
        if follow_up["status"] != "OPEN" or follow_up["category"] != "CUSTOMER_INFORMATION":
            raise HTTPException(status_code=409, detail="This follow-up is no longer waiting for customer information.")
        connection.execute(
            "UPDATE job_follow_ups SET status='RECEIVED',resolution=?,"
            "received_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (resolution, follow_up_id),
        )
        message = f"Customer information received — review required: {follow_up['summary']}"
        write_audit(
            connection, action="CUSTOMER_INFORMATION_RECEIVED",
            entity_type="JOB_FOLLOW_UP", entity_id=follow_up_id,
            summary=message, metadata={"job_id": follow_up["job_id"]},
        )
        log_job_event(
            connection, job_id=int(follow_up["job_id"]),
            event_type="CUSTOMER_INFORMATION_RECEIVED", icon="!", message=message,
        )
        connection.commit()
    return RedirectResponse(url=f"/jobs/{follow_up['job_id']}/basket#follow-ups", status_code=303)

@router.post("/follow-ups/{follow_up_id}/resolve")
def resolve_follow_up(
    follow_up_id: int,
    resolution: Annotated[str, Form()],
):
    resolution = _required(resolution, "Resolution")
    with closing(get_connection()) as connection:
        follow_up = _open_follow_up(connection, follow_up_id)
        if follow_up["status"] == "RESOLVED":
            return RedirectResponse(
                url=f"/jobs/{follow_up['job_id']}/basket#follow-ups", status_code=303
            )
        if follow_up["status"] == "CANCELLED":
            raise HTTPException(status_code=409, detail="Cancelled follow-up cannot be resolved.")
        connection.execute(
            "UPDATE job_follow_ups SET status='RESOLVED',resolution=?,"
            "resolved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (resolution, follow_up_id),
        )
        message = f"Follow-up resolved: {follow_up['summary']}"
        write_audit(
            connection, action="JOB_FOLLOW_UP_RESOLVED",
            entity_type="JOB_FOLLOW_UP", entity_id=follow_up_id,
            summary=message, metadata={"job_id": follow_up["job_id"]},
        )
        log_job_event(
            connection, job_id=int(follow_up["job_id"]),
            event_type="JOB_FOLLOW_UP_RESOLVED", icon="✓", message=message,
        )
        connection.commit()
    return RedirectResponse(url=f"/jobs/{follow_up['job_id']}/basket#follow-ups", status_code=303)
