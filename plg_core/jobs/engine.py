from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


def _value(record: Any, key: str, default: Any = None) -> Any:
    if record is None:
        return default
    if isinstance(record, Mapping):
        return record.get(key, default)
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return getattr(record, key, default)


def _present(value: Any) -> bool:
    return bool(str(value or "").strip())


@dataclass(frozen=True)
class JobIntelligence:
    next_action: str
    action_key: str
    action_url: str
    action_method: str
    workflow_stage: str
    health: str
    health_label: str
    workflow_steps: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workflow_steps"] = list(self.workflow_steps)
        return data


class JobEngine:
    """Calculates the operational state of a PLG job from existing records.

    The engine is deliberately read-only. It does not update the database and it
    keeps workflow rules in one place so the Command Center, Jobs list, and later
    the Dashboard can all display the same answer.
    """

    STAGES = ("REQUEST", "CUSTOMER", "REGISTRY", "RESEARCH", "QUOTE", "INVOICE", "COMPLETE")

    @classmethod
    def evaluate(
        cls,
        job: Any,
        *,
        selected_items: int = 0,
        basket_status: str = "OPEN",
        customer_request: Any = None,
        quote: Any = None,
        invoice: Any = None,
    ) -> JobIntelligence:
        job_id = int(_value(job, "id", 0) or 0)
        customer_id = _value(job, "customer_id")
        machine_id = _value(job, "machine_id")

        has_request = customer_request is not None or bool(_value(job, "customer_request_id"))
        has_customer = bool(customer_id) or _present(_value(job, "customer")) or _present(_value(job, "company"))
        has_registry = bool(machine_id) or any(
            _present(_value(job, field)) for field in ("manufacturer", "machine", "pin_serial")
        )
        has_parts = int(selected_items or 0) > 0
        basket_committed = str(basket_status or "").upper() == "COMMITTED"
        has_quote = quote is not None or bool(_value(job, "quote_id"))
        quote_status = str(_value(quote, "status", _value(job, "quote_status", "")) or "").upper()
        has_invoice = invoice is not None or bool(_value(job, "invoice_id"))
        invoice_status = str(_value(invoice, "status", _value(job, "invoice_status", "")) or "").upper()
        job_status = str(_value(job, "status", "") or "").upper()

        completed = invoice_status == "PAID" or job_status in {"DELIVERED", "COMPLETED", "COMPLETE"}

        if not has_customer:
            action = ("Link Customer", "LINK_CUSTOMER", f"/jobs/{job_id}/edit", "GET")
            stage = "CUSTOMER"
        elif not has_registry:
            action = ("Link Registry", "LINK_REGISTRY", f"/jobs/{job_id}/edit", "GET")
            stage = "REGISTRY"
        elif not has_parts:
            action = ("Research Parts", "RESEARCH_PARTS", "#parts-research", "GET")
            stage = "RESEARCH"
        elif not has_quote and not basket_committed:
            action = ("Review Selected Parts", "REVIEW_PARTS", "#parts-ready", "GET")
            stage = "RESEARCH"
        elif not has_quote:
            action = ("Create Quote", "CREATE_QUOTE", f"/jobs/{job_id}/generate-quote", "POST")
            stage = "QUOTE"
        elif has_quote and not has_invoice:
            if quote_status in {"DRAFT", ""}:
                action = ("Review Quote", "REVIEW_QUOTE", f"/quotes/{_value(quote, 'id', _value(job, 'quote_id', ''))}/documents", "GET")
            else:
                action = ("Waiting for Customer", "WAITING_CUSTOMER", "/quotes", "GET")
            stage = "QUOTE"
        elif completed:
            action = ("Completed", "COMPLETED", f"/invoices/{_value(invoice, 'id', _value(job, 'invoice_id', ''))}/documents", "GET")
            stage = "COMPLETE"
        elif invoice_status in {"UNPAID", "PARTIAL", ""}:
            action = ("Waiting for Payment", "WAITING_PAYMENT", f"/invoices/{_value(invoice, 'id', _value(job, 'invoice_id', ''))}/documents", "GET")
            stage = "INVOICE"
        else:
            action = ("Review Invoice", "REVIEW_INVOICE", f"/invoices/{_value(invoice, 'id', _value(job, 'invoice_id', ''))}/documents", "GET")
            stage = "INVOICE"

        if action[1] in {"WAITING_CUSTOMER", "WAITING_PAYMENT"}:
            health, health_label = "WAITING", "Waiting"
        elif action[1] == "COMPLETED":
            health, health_label = "COMPLETE", "Complete"
        elif action[1] in {"LINK_CUSTOMER", "LINK_REGISTRY"}:
            health, health_label = "ACTION_REQUIRED", "Needs attention"
        else:
            health, health_label = "HEALTHY", "Ready"

        current_index = cls.STAGES.index(stage)
        completed_flags = {
            "REQUEST": has_request,
            "CUSTOMER": has_customer,
            "REGISTRY": has_registry,
            "RESEARCH": has_parts,
            "QUOTE": has_quote,
            "INVOICE": has_invoice,
            "COMPLETE": completed,
        }
        urls = {
            "REQUEST": f"/requests/{_value(customer_request, 'id', _value(job, 'customer_request_id', ''))}" if has_request else "",
            "CUSTOMER": f"/customers/{customer_id}" if customer_id else "/customers",
            "REGISTRY": f"/machines/{machine_id}" if machine_id else "/machines",
            "RESEARCH": "#parts-research",
            "QUOTE": f"/quotes/{_value(quote, 'id', _value(job, 'quote_id', ''))}/documents" if has_quote else "",
            "INVOICE": f"/invoices/{_value(invoice, 'id', _value(job, 'invoice_id', ''))}/documents" if has_invoice else "",
            "COMPLETE": "",
        }

        steps: list[dict[str, Any]] = []
        for index, name in enumerate(cls.STAGES):
            is_done = bool(completed_flags[name]) or index < current_index
            is_current = name == stage and not completed
            steps.append(
                {
                    "key": name,
                    "label": name.title(),
                    "state": "done" if is_done else "current" if is_current else "upcoming",
                    "symbol": "✓" if is_done else "●" if is_current else "○",
                    "url": urls[name],
                }
            )

        return JobIntelligence(
            next_action=action[0],
            action_key=action[1],
            action_url=action[2],
            action_method=action[3],
            workflow_stage=stage,
            health=health,
            health_label=health_label,
            workflow_steps=tuple(steps),
        )
