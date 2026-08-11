from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


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


def _status(value: Any) -> str:
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class JobIntelligence:
    next_action: str
    action_key: str
    action_url: str
    action_method: str

    workflow_stage: str
    workflow_label: str

    health: str
    health_label: str

    priority: str
    progress_percent: int
    blocked_reason: str

    selected_items: int
    research_items: int
    quoted_items: int
    ordered_items: int
    received_items: int
    outstanding_count: int
    outstanding_parts: tuple[str, ...]

    workflow_steps: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workflow_steps"] = list(self.workflow_steps)
        data["outstanding_parts"] = list(self.outstanding_parts)
        return data


class JobEngine:
    """Central source of truth for the operational state of a PLG job."""

    STAGES = (
        "REQUEST",
        "CUSTOMER",
        "REGISTRY",
        "RESEARCH",
        "READY_TO_QUOTE",
        "WAITING_CUSTOMER",
        "WAITING_PAYMENT",
        "READY_TO_ORDER",
        "WAITING_PARTS",
        "READY_TO_COMPLETE",
        "COMPLETE",
    )

    STAGE_LABELS = {
        "REQUEST": "Customer Request",
        "CUSTOMER": "Customer",
        "REGISTRY": "Registry",
        "RESEARCH": "Research",
        "READY_TO_QUOTE": "Ready to Quote",
        "WAITING_CUSTOMER": "Waiting for Customer",
        "WAITING_PAYMENT": "Waiting for Payment",
        "READY_TO_ORDER": "Ready to Order",
        "WAITING_PARTS": "Waiting for Parts",
        "READY_TO_COMPLETE": "Ready for Delivery",
        "COMPLETE": "Completed",
    }

    STAGE_PROGRESS = {
        "REQUEST": 5,
        "CUSTOMER": 12,
        "REGISTRY": 20,
        "RESEARCH": 32,
        "READY_TO_QUOTE": 45,
        "WAITING_CUSTOMER": 55,
        "WAITING_PAYMENT": 65,
        "READY_TO_ORDER": 75,
        "WAITING_PARTS": 85,
        "READY_TO_COMPLETE": 95,
        "COMPLETE": 100,
    }

    @classmethod
    def evaluate(
        cls,
        job: Any,
        *,
        selected_items: int = 0,
        research_items: int = 0,
        quoted_items: int = 0,
        ordered_items: int = 0,
        received_items: int = 0,
        outstanding_parts: Sequence[str] | None = None,
        basket_status: str = "OPEN",
        customer_request: Any = None,
        quote: Any = None,
        invoice: Any = None,
    ) -> JobIntelligence:
        job_id = int(_value(job, "id", 0) or 0)
        customer_id = _value(job, "customer_id")
        machine_id = _value(job, "machine_id")

        selected_items = max(int(selected_items or 0), 0)
        research_items = max(int(research_items or 0), 0)
        quoted_items = max(int(quoted_items or 0), 0)
        ordered_items = max(int(ordered_items or 0), 0)
        received_items = max(int(received_items or 0), 0)

        outstanding = tuple(
            str(name).strip()
            for name in (outstanding_parts or ())
            if str(name or "").strip()
        )

        outstanding_count = max(selected_items - received_items, 0)

        has_customer = (
            bool(customer_id)
            or _present(_value(job, "customer"))
            or _present(_value(job, "company"))
        )

        has_registry = bool(machine_id) or any(
            _present(_value(job, field))
            for field in ("manufacturer", "machine", "pin_serial")
        )

        has_linked_request = (
            customer_request is not None
            or bool(_value(job, "customer_request_id"))
        )

        # PLG supports two valid job entry paths:
        #
        # 1. A Job created from a Customer Request.
        # 2. A manually created Job with customer and registry data.
        #
        # Manual Jobs must not remain permanently blocked at the
        # Customer Request stage.
        has_request = (
            has_linked_request
            or (has_customer and has_registry)
        )

        has_parts = selected_items > 0
        basket_committed = _status(basket_status) == "COMMITTED"

        has_quote = (
            quote is not None
            or bool(_value(job, "quote_id"))
        )

        quote_id = _value(
            quote,
            "id",
            _value(job, "quote_id", ""),
        )

        quote_status = _status(
            _value(
                quote,
                "status",
                _value(job, "quote_status", ""),
            )
        )

        quote_approved = quote_status in {
            "APPROVED",
            "ACCEPTED",
            "CONFIRMED",
        }

        has_invoice = (
            invoice is not None
            or bool(_value(job, "invoice_id"))
        )

        invoice_id = _value(
            invoice,
            "id",
            _value(job, "invoice_id", ""),
        )

        invoice_status = _status(
            _value(
                invoice,
                "status",
                _value(job, "invoice_status", ""),
            )
        )

        payment_received = invoice_status in {
            "PAID",
            "PAYMENT RECEIVED",
            "PAYMENT_RECEIVED",
        }

        job_status = _status(_value(job, "status", ""))

        # Supplier POs are the durable purchasing source of truth.
        # Once the job reaches ORDERED or RECEIVED, do not let
        # stale basket part_status values move the command center
        # backward in the workflow.
        if job_status == "ORDERED":
            ordered_items = max(
                ordered_items,
                selected_items,
            )

        elif job_status == "RECEIVED":
            ordered_items = max(
                ordered_items,
                selected_items,
            )
            received_items = max(
                received_items,
                selected_items,
            )

        completed = job_status in {
            "DELIVERED",
            "COMPLETED",
            "COMPLETE",
            "CLOSED",
        }

        if not has_request:
            stage = "REQUEST"
            action = (
                "Create Customer Request",
                "CREATE_REQUEST",
                "/requests/new",
                "GET",
            )
            blocked_reason = "No customer request is linked to this job."

        elif not has_customer:
            stage = "CUSTOMER"
            action = (
                "Link Customer",
                "LINK_CUSTOMER",
                f"/jobs/{job_id}/edit",
                "GET",
            )
            blocked_reason = "Customer information is missing."

        elif not has_registry:
            stage = "REGISTRY"
            action = (
                "Link Registry",
                "LINK_REGISTRY",
                f"/jobs/{job_id}/edit",
                "GET",
            )
            blocked_reason = "Machine or registry information is missing."

        elif not has_parts:
            stage = "RESEARCH"
            action = (
                "Research Parts",
                "RESEARCH_PARTS",
                "#parts-research",
                "GET",
            )
            blocked_reason = "No parts have been selected."

        elif research_items > 0:
            stage = "RESEARCH"
            action = (
                f"Research {research_items} remaining "
                f"part{'s' if research_items != 1 else ''}",
                "RESEARCH_REMAINING_PARTS",
                "#parts-research",
                "GET",
            )
            blocked_reason = (
                f"{research_items} selected "
                f"part{'s are' if research_items != 1 else ' is'} "
                "still being researched."
            )

        elif not has_quote:
            stage = "READY_TO_QUOTE"

            if not basket_committed:
                action = (
                    "Review Selected Parts",
                    "REVIEW_PARTS",
                    "#parts-ready",
                    "GET",
                )
                blocked_reason = (
                    "Selected parts need final review before quoting."
                )
            else:
                action = (
                    "Create Quote",
                    "CREATE_QUOTE",
                    f"/jobs/{job_id}/generate-quote",
                    "POST",
                )
                blocked_reason = "The job is ready for a customer quote."

        elif has_quote and quote_status == "REVISION_REQUIRED":
            stage = "READY_TO_QUOTE"
            action = (
                "Revise Quote",
                "REVISE_QUOTE",
                f"/jobs/{job_id}/basket",
                "GET",
            )
            blocked_reason = (
                "The customer requested changes to the quote."
            )

        elif has_quote and quote_status == "REJECTED":
            stage = "READY_TO_QUOTE"
            action = (
                "Review Rejected Quote",
                "REVIEW_REJECTED_QUOTE",
                f"/jobs/{job_id}/basket",
                "GET",
            )
            blocked_reason = (
                "The customer rejected the quote. Review pricing "
                "or close the job."
            )

        elif has_quote and not quote_approved:
            stage = "WAITING_CUSTOMER"

            if quote_status in {"", "DRAFT"}:
                action = (
                    "Review Quote",
                    "REVIEW_QUOTE",
                    f"/quotes/{quote_id}/documents",
                    "GET",
                )
                blocked_reason = "The quote has not been sent or finalized."
            else:
                action = (
                    "Waiting for Customer",
                    "WAITING_CUSTOMER",
                    f"/quotes/{quote_id}/documents",
                    "GET",
                )
                blocked_reason = "Waiting for customer approval."

        elif quote_approved and not payment_received:
            stage = "WAITING_PAYMENT"

            if has_invoice:
                action = (
                    "Waiting for Payment",
                    "WAITING_PAYMENT",
                    f"/invoices/{invoice_id}/documents",
                    "GET",
                )
            else:
                action = (
                    "Create Invoice for Payment",
                    "CREATE_PAYMENT_INVOICE",
                    f"/quotes/{quote_id}/convert-to-invoice",
                    "POST",
                )

            blocked_reason = (
                "Customer approval is recorded, but payment "
                "has not been received."
            )

        elif payment_received and completed:
            stage = "COMPLETE"
            action = (
                "Completed",
                "COMPLETED",
                f"/invoices/{invoice_id}/documents",
                "GET",
            )
            blocked_reason = ""

        elif payment_received and (
            job_status == "RECEIVED"
            or (
                selected_items > 0
                and received_items >= selected_items
            )
        ):
            stage = "READY_TO_COMPLETE"
            action = (
                "Prepare Delivery",
                "PREPARE_DELIVERY",
                f"/jobs/{job_id}/delivery",
                "GET",
            )
            blocked_reason = (
                "Purchased parts are received and ready "
                "for customer delivery."
            )

        elif payment_received and (
            job_status == "ORDERED"
            or ordered_items > 0
        ):
            stage = "WAITING_PARTS"

            remaining = max(
                selected_items - received_items,
                0,
            )

            action = (
                f"Receive {remaining} remaining "
                f"part{'s' if remaining != 1 else ''}",
                "RECEIVE_REMAINING_PARTS",
                "/purchasing",
                "GET",
            )

            blocked_reason = (
                f"Waiting for {remaining} ordered "
                f"part{'s' if remaining != 1 else ''}."
            )

        else:
            stage = "READY_TO_ORDER"

            orderable = max(
                selected_items - ordered_items - received_items,
                0,
            )

            action = (
                f"Order {orderable} "
                f"part{'s' if orderable != 1 else ''}",
                "ORDER_PARTS",
                "#parts-ready",
                "GET",
            )

            blocked_reason = (
                "Payment has been received. Parts are ready to order."
            )

        if stage in {"CUSTOMER", "REGISTRY"}:
            health = "ACTION_REQUIRED"
            health_label = "Needs attention"
            priority = "URGENT"

        elif stage in {
            "WAITING_CUSTOMER",
            "WAITING_PAYMENT",
            "WAITING_PARTS",
        }:
            health = "WAITING"
            health_label = "Waiting"
            priority = "FOLLOW_UP"

        elif stage == "COMPLETE":
            health = "COMPLETE"
            health_label = "Complete"
            priority = "COMPLETE"

        elif stage in {
            "READY_TO_QUOTE",
            "READY_TO_ORDER",
            "READY_TO_COMPLETE",
        }:
            health = "READY"
            health_label = "Ready"
            priority = "READY"

        else:
            health = "HEALTHY"
            health_label = "In progress"
            priority = "TODAY"

        completed_flags = {
            "REQUEST": has_request,
            "CUSTOMER": has_customer,
            "REGISTRY": has_registry,
            "RESEARCH": has_parts and research_items == 0,
            "READY_TO_QUOTE": has_quote,
            "WAITING_CUSTOMER": quote_approved,
            "WAITING_PAYMENT": payment_received,
            "READY_TO_ORDER": ordered_items > 0 or received_items > 0,
            "WAITING_PARTS": (
                selected_items > 0
                and received_items >= selected_items
            ),
            "READY_TO_COMPLETE": completed,
            "COMPLETE": completed,
        }

        urls = {
            "REQUEST": (
                f"/requests/{_value(customer_request, 'id', _value(job, 'customer_request_id', ''))}"
                if has_request
                else "/requests/new"
            ),
            "CUSTOMER": (
                f"/customers/{customer_id}"
                if customer_id
                else "/customers"
            ),
            "REGISTRY": (
                f"/machines/{machine_id}"
                if machine_id
                else "/machines"
            ),
            "RESEARCH": "#parts-research",
            "READY_TO_QUOTE": "#parts-ready",
            "WAITING_CUSTOMER": (
                f"/quotes/{quote_id}/documents"
                if has_quote
                else ""
            ),
            "WAITING_PAYMENT": (
                f"/invoices/{invoice_id}/documents"
                if has_invoice
                else ""
            ),
            "READY_TO_ORDER": "/purchasing",
            "WAITING_PARTS": "/purchasing",
            "READY_TO_COMPLETE": f"/jobs/{job_id}/delivery",
            "COMPLETE": "",
        }

        current_index = cls.STAGES.index(stage)
        steps: list[dict[str, Any]] = []

        for index, name in enumerate(cls.STAGES):
            is_done = (
                bool(completed_flags[name])
                or index < current_index
            )

            is_current = (
                name == stage
                and not completed
            )

            steps.append(
                {
                    "key": name,
                    "label": cls.STAGE_LABELS[name],
                    "state": (
                        "done"
                        if is_done
                        else "current"
                        if is_current
                        else "upcoming"
                    ),
                    "symbol": (
                        "✓"
                        if is_done
                        else "●"
                        if is_current
                        else "○"
                    ),
                    "url": urls[name],
                }
            )

        return JobIntelligence(
            next_action=action[0],
            action_key=action[1],
            action_url=action[2],
            action_method=action[3],
            workflow_stage=stage,
            workflow_label=cls.STAGE_LABELS[stage],
            health=health,
            health_label=health_label,
            priority=priority,
            progress_percent=cls.STAGE_PROGRESS[stage],
            blocked_reason=blocked_reason,
            selected_items=selected_items,
            research_items=research_items,
            quoted_items=quoted_items,
            ordered_items=ordered_items,
            received_items=received_items,
            outstanding_count=outstanding_count,
            outstanding_parts=outstanding,
            workflow_steps=tuple(steps),
        )
