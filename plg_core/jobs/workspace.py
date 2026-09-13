"""Read-only operator projection. Loading a job never repairs or creates records."""
from contextlib import closing
import re
from urllib.parse import quote_plus

from fastapi import HTTPException

from legacy_app import get_connection, templates
from plg_core.basket.service import serialize_basket
from plg_core.jobs.service import get_job_operational_snapshot
from plg_core.pricing import pricing_assessment
from plg_core.sources.service import list_sources_for_context, validate_source_url
from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request


def _safe_url(value):
    try:
        return validate_source_url(value or "")
    except HTTPException:
        return ""




def derive_v2_stage(snapshot: dict) -> str:
    """Return a concise non-persisted stage distinct from the next action."""
    quote = snapshot.get("quote") or {}
    invoice = snapshot.get("invoice") or {}
    movement = snapshot.get("movement") or {}
    ordered = int(movement.get("ordered_units") or 0)
    invoice_paid = bool(invoice) and (str(invoice.get("status") or "").upper() == "PAID" or float(invoice.get("balance_due") or 0) <= 0)
    if invoice_paid and str(quote.get("status") or "").upper() == "CONVERTED":
        return {"stage": "Complete", "next_action": "Complete", "next_url": snapshot.get("delivery_url") or "#", "action_method": "GET"}
    received = int(movement.get("received_units") or 0)
    delivered = int(movement.get("delivered_units") or 0)
    if ordered and delivered >= ordered:
        return "Billing" if invoice else "Fulfillment"
    if ordered:
        return "Fulfillment"
    status = str(quote.get("status") or "").upper()
    if status == "SENT":
        return "Awaiting Customer"
    if quote:
        return "Quote"
    return "Sourcing"

def derive_v2_workflow(snapshot: dict, parts: list[dict], basket: dict) -> dict:
    """Translate authoritative lifecycle evidence into operator-facing V2 language."""
    quote = snapshot.get("quote") or {}
    invoice = snapshot.get("invoice") or {}
    movement = snapshot.get("movement") or {}
    orders = snapshot.get("supplier_orders") or []
    selected = [part for part in parts if part.get("current_options", part.get("selected_options"))]
    priced = all((part.get("current_options", part["selected_options"])[0].get("supplier_unit_cost") is not None) for part in selected)
    all_ready = bool(parts) and len(selected) == len(parts) and priced
    ordered = int(movement.get("ordered_units") or 0)
    invoice_paid = bool(invoice) and (str(invoice.get("status") or "").upper() == "PAID" or float(invoice.get("balance_due") or 0) <= 0)
    if invoice_paid and str(quote.get("status") or "").upper() == "CONVERTED":
        return {"stage": "Complete", "next_action": "Complete", "next_url": snapshot.get("delivery_url") or "#", "action_method": "GET"}
    received = int(movement.get("received_units") or 0)
    delivered = int(movement.get("delivered_units") or 0)
    if ordered and delivered >= ordered:
        paid = str(invoice.get("payment_state") or invoice.get("status") or "").upper() == "PAID" or float(invoice.get("balance_due") or 0) <= 0 if invoice else False
        return {"stage": derive_v2_stage(snapshot), "next_action": "Complete" if paid else "Payment due", "next_url": snapshot.get("delivery_url") or "#", "action_method": "GET"}
    if ordered and received >= ordered and delivered < ordered:
        return {"stage": derive_v2_stage(snapshot), "next_action": "Ready to deliver", "next_url": snapshot.get("delivery_url") or "#", "action_method": "GET"}
    if ordered and received < ordered:
        return {"stage": derive_v2_stage(snapshot), "next_action": "Waiting on supplier", "next_url": snapshot.get("workflow", {}).get("next_url") or "/purchasing", "action_method": "GET"}
    status = str(quote.get("status") or "").upper()
    if status == "APPROVED" and not orders:
        return {"stage": derive_v2_stage(snapshot), "next_action": "Ready to order", "next_url": snapshot.get("workflow", {}).get("next_url") or "/purchasing", "action_method": "GET"}
    if status == "SENT":
        return {"stage": derive_v2_stage(snapshot), "next_action": "Waiting for customer", "next_url": snapshot.get("workflow", {}).get("next_url") or "#quote", "action_method": "GET"}
    if quote and status in {"DRAFT", "REVISION_REQUIRED"}:
        return {"stage": derive_v2_stage(snapshot), "next_action": "Ready to send", "next_url": "#quote", "action_method": "GET"}
    if all_ready:
        return {"stage": derive_v2_stage(snapshot), "next_action": "Ready to quote", "next_url": "#quote", "action_method": "GET"}
    return {"stage": derive_v2_stage(snapshot), "next_action": "Needs supplier", "next_url": "#parts", "action_method": "GET"}

def build_workspace(connection, job_id):
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found.")
    job = dict(row)
    snapshot = get_job_operational_snapshot(job_id, connection=connection)
    for event in snapshot["activity"]:
        event["display_message"] = re.sub(
            r"research result|quote candidate|research candidate", "option", event["message"], flags=re.I
        )
    basket_row = connection.execute("SELECT * FROM baskets WHERE job_id=?", (job_id,)).fetchone()
    basket = serialize_basket(connection, basket_row) if basket_row else {
        "id": None, "status": "OPEN", "currency": "USD", "items": [], "sources": [],
        "work_revision": None, "totals": {"customer_total": 0, "shipping_total": 0},
    }
    revision_row = connection.execute(
        "SELECT * FROM work_revisions WHERE id=? AND job_id=?",
        (job.get("active_work_revision_id"), job_id),
    ).fetchone()
    revision = dict(revision_row) if revision_row else None
    durable = bool(snapshot["quote"] or snapshot["invoice"] or snapshot["supplier_orders"])
    editable = (
        job["status"].upper() not in {"CANCELLED", "ARCHIVED", "DELIVERED", "COMPLETED", "COMPLETE", "CLOSED"}
        and not job.get("is_archived")
        and not snapshot["invoice"]
        and (revision["state"] == "EDITABLE" if revision else basket["status"] != "COMMITTED")
        and not (durable and (not revision or revision["is_synthetic"]))
    )
    assets = {asset["id"]: asset for asset in snapshot["assets"]}
    sources = {source["id"]: source for source in basket["sources"]}
    links = {}
    preferred_links = {}
    for link in connection.execute(
        "SELECT l.* FROM basket_item_need_links l JOIN basket_items i ON i.id=l.basket_item_id "
        "JOIN baskets b ON b.id=i.basket_id WHERE b.job_id=?", (job_id,),
    ):
        links.setdefault(link["basket_item_id"], set()).add(link["requested_need_id"])
        if link["preferred"]:
            preferred_links.setdefault(link["requested_need_id"], []).append(link["basket_item_id"])
    shipping = {row["basket_item_id"]: dict(row) for row in connection.execute(
        "SELECT s.* FROM part_shipping_data s JOIN basket_items i ON i.id=s.basket_item_id "
        "JOIN baskets b ON b.id=i.basket_id WHERE b.job_id=? AND s.is_current=1 ORDER BY s.id",
        (job_id,),
    )}
    options = []
    for stored in basket["items"]:
        option = dict(stored)
        ids = set(links.get(option["id"], set()))
        if option.get("primary_requested_need_id"):
            ids.add(option["primary_requested_need_id"])
        source = sources.get(option.get("source_id"), {})
        option.update(
            need_ids=sorted(ids), source=source,
            currency=source.get("currency") or basket["currency"],
            safe_url=_safe_url(option.get("source_url")),
            shipping=shipping.get(option["id"], {}),
            shared=len(ids) > 1,
            pricing=pricing_assessment(option.get("supplier_unit_cost") or 0,
                                       option.get("markup_percent"), option.get("customer_unit_price_override")),
            compatibility_warning=("Compatibility rejected — review before use" if option.get("verification_status") == "REJECTED"
                                   else "Compatibility needs review" if option.get("verification_status")
                                   in {"NEEDS_REVIEW", "UNVERIFIED", "PROVISIONAL"} else ""),
        )
        options.append(option)
    parts, attached = [], set()
    for need in snapshot["needs"]:
        part = dict(need)
        part_options = [item for item in options if need["id"] in item["need_ids"]]
        explicit = [item for item in part_options if item["id"] in preferred_links.get(need["id"], [])]
        legacy_selected = [item for item in part_options if item["selected"]]
        current = explicit if len(explicit) == 1 else (legacy_selected if not explicit and len(legacy_selected) == 1 else [])
        attached.update(item["id"] for item in part_options)
        # No exclusive replacement or kit allocation is inferred in this phase.
        selected = legacy_selected
        ambiguous = len(selected) > 1 or any(
            item["shared"] or (need["job_asset_id"] is not None and item["job_asset_id"] != need["job_asset_id"])
            for item in part_options
        )
        part.update(options=part_options, selected_options=legacy_selected, current_options=current,
                    preferred_option_ids={item["id"] for item in current}, asset=assets.get(need["job_asset_id"]),
                    ambiguous=ambiguous, suggested=None)
        part["status_label"] = (
            "Multiple selections · review quantities" if len(selected) > 1 else
            "Selected option" if selected else
            "Marked covered" if need["state"] == "SATISFIED" else
            f"{len(part_options)} option{'s' if len(part_options) != 1 else ''} found" if part_options else "Need sourcing"
        )
        # An option can only be suggested when price, availability and fitment are
        # comparable. Shipping is shared/unknown, so never claim a landed-cost best.
        priced = [item for item in part_options if item["supplier_unit_cost"] is not None
                  and str(item.get("availability") or "").strip().lower() == "in stock"
                  and item.get("verification_status") == "VERIFIED"]
        if not selected and not ambiguous and priced and len({item["currency"] for item in priced}) == 1:
            part["suggested"] = min(priced, key=lambda item: (item["supplier_unit_cost"], item["id"]))
        parts.append(part)
    unassigned = [item for item in options if item["id"] not in attached]
    selected = [item for item in options if item["selected"] and
                (item.get("research_state") or "LEGACY_CANDIDATE") in {"QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}]
    needs_options = sum(not part.get("current_options", []) and part["state"] == "OPEN" for part in parts)
    outstanding = f"{needs_options} requested part{'s' if needs_options != 1 else ''} still need an option"
    if parts and not needs_options:
        outstanding = "All requested parts have a selection or are marked covered"
    if snapshot["invoice"]:
        movement = snapshot["movement"]
        outstanding = (f"{movement['remaining_units']} units awaiting receipt · "
                       f"{movement['available_to_deliver_units']} available to deliver")
        if snapshot["invoice"]["balance_due"] > 0:
            outstanding = "Payment needed before ordering"
    elif not parts:
        outstanding = "Add what the customer needs" if not options else "Review other job items"
    action = dict(snapshot["workflow"])
    # Navigation into the new sections; commercial mutations still use their
    # existing forms/services and authoritative HTTP methods.
    if "#" in action["next_url"] and "/basket" in action["next_url"]:
        action["next_url"] = "#parts"
        action["next_action"] = "View Parts"
    if action["next_url"].endswith("/generate-quote") and not snapshot["quote"]:
        action.update(next_url="#quote", next_action="Review Quote", action_method="GET")
    if action["next_url"] == "/purchasing":
        action.update(next_url="#orders")
    if action["stage"] == "Research":
        action["stage"] = "Finding parts"
    if editable and not durable:
        if not parts and not options:
            action.update(next_url="#add-part", next_action="Add Requested Part", action_method="GET")
        elif not selected:
            action.update(next_url="#parts", next_action="Find / Add Options", action_method="GET")
        else:
            action.update(next_url="#quote", next_action="Review Quote", action_method="GET")
    line_items = []
    projected_customer_total = 0.0
    projected_estimated_cost = 0.0
    customer_total_complete = bool(parts)
    estimated_cost_complete = bool(parts)
    for part in parts:
        selected_option = part.get("current_options", [])[0] if part.get("current_options") else None
        if selected_option:
            qty = selected_option.get("quantity") or 1
            supplier_unit_cost = selected_option.get("supplier_unit_cost")
            cost = (supplier_unit_cost or 0) * qty
            sell = (selected_option.get("pricing", {}).get("current_unit_price") or 0) * qty
            if supplier_unit_cost is None:
                estimated_cost_complete = False
            else:
                projected_estimated_cost += cost
            if selected_option.get("pricing", {}).get("current_unit_price") is None:
                customer_total_complete = False
            else:
                projected_customer_total += sell
            supplier = selected_option.get("supplier_name") or "Supplier not recorded"
            status = "Selected"
            next_action = "Ready for quote"
        else:
            qty = part.get("quantity")
            cost = sell = 0
            customer_total_complete = False
            estimated_cost_complete = False
            supplier = "Supplier needed"
            status = part["status_label"]
            next_action = "Review sourcing"
        line_items.append({"id": part["id"], "description": part["wording"], "quantity": qty,
                           "supplier": supplier, "actual_cost": round(cost, 2),
                           "sell_price": round(sell, 2), "status": status,
                           "next_action": next_action, "options": part.get("options", []),
                           "preferred_option_ids": set(part.get("preferred_option_ids", set())),
                           "selected": bool(selected_option),
                           "pricing_editable": bool(selected_option and selected_option["id"] in preferred_links.get(part["id"], [])),
                           "cost_known": selected_option is not None and selected_option.get("supplier_unit_cost") is not None,
                           "selected_option": selected_option,
                           "pricing": selected_option.get("pricing", {}) if selected_option else {}})
    all_actual_confirmed = bool(snapshot.get("supplier_orders")) and all(
        o.get("actual_cost_state") == "CONFIRMED" for o in snapshot["supplier_orders"]
    )
    actual_cost = round(sum(float(o.get("actual_cost") or 0) for o in snapshot.get("supplier_orders", [])), 2)
    if all_actual_confirmed:
        realized_revenue = float(snapshot.get("invoice", {}).get("customer_total") or projected_customer_total) if snapshot.get("invoice") else projected_customer_total
        financial_projection = {
            "mode": "REALIZED", "customer_total": round(realized_revenue, 2),
            "cost": actual_cost, "profit": round(realized_revenue - actual_cost, 2),
            "complete": True, "customer_total_complete": True,
            "estimated_cost_complete": True,
        }
    else:
        financial_projection = {
            "mode": "PROJECTED", "customer_total": round(projected_customer_total, 2),
            "cost": round(projected_estimated_cost, 2),
            "profit": round(projected_customer_total - projected_estimated_cost, 2) if customer_total_complete and estimated_cost_complete else None,
            "complete": customer_total_complete and estimated_cost_complete,
            "customer_total_complete": customer_total_complete,
            "estimated_cost_complete": estimated_cost_complete,
        }
    v2_workflow = derive_v2_workflow(snapshot, parts, basket)
    customers = [dict(row) for row in connection.execute(
        "SELECT * FROM customers WHERE active=1 OR id=? ORDER BY name COLLATE NOCASE",
        (job.get("customer_id") or -1,),
    )]
    machines = [dict(row) for row in connection.execute(
        "SELECT * FROM machines WHERE active=1 OR id=? ORDER BY name COLLATE NOCASE",
        (job.get("machine_id") or -1,),
    )]
    return dict(job=job, operational_snapshot=snapshot, v2_workflow=v2_workflow, basket=basket, revision=revision,
                work_editable=editable, parts=parts, line_items=line_items, other_options=unassigned,
                selected_options=selected, outstanding=outstanding, primary_action=action,
                customers=customers, machines=machines, financial_projection=financial_projection,
                quote_history=[dict(row) for row in connection.execute(
                    "SELECT * FROM quotes WHERE job_id=? ORDER BY id DESC", (job_id,))],
                legacy_parts=[dict(row) for row in connection.execute(
                    "SELECT id,requested_description,quantity FROM job_parts WHERE job_id=? ORDER BY id", (job_id,))])


def render_workspace(request, job_id, *, need_id=None):
    with closing(get_connection()) as connection:
        # A read transaction gives all cards a consistent view without any writes.
        connection.execute("BEGIN")
        context = build_workspace(connection, job_id)
        focus = next((part for part in context["parts"] if part["id"] == need_id), None)
        if need_id is not None and focus is None:
            raise HTTPException(404, "Requested part not found in this job.")
        destinations = []
        if focus and focus["asset"]:
            asset = focus["asset"]
            destinations = list_sources_for_context(
                connection, manufacturer=asset.get("manufacturer") or "",
                asset_category=asset.get("asset_type") or "other",
                market=asset.get("market_region") or "UNKNOWN",
            )
        context.update(focused_part=focus, destinations=destinations,
                       search_url="https://www.google.com/search?q=" + quote_plus(focus["wording"]) if focus else "")
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(request=request, name="job_command_center.html", context={
        **context, "csrf_token": token, "active_page": "jobs",
    })
    response.set_cookie(CSRF_COOKIE_NAME, token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return response


def render_job_center_v2(request, job_id: int, *, tab="job", message: str = ""):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN")
        context = build_workspace(connection, job_id)
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(request=request, name="job_center_v2.html", context={
        **context, "csrf_token": token, "active_page": "jobs",
        "message": message,
        "active_tab": tab if tab in {"job", "quote", "purchasing", "fulfillment", "documents"} else "job",
    })
    response.set_cookie(CSRF_COOKIE_NAME, token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response
