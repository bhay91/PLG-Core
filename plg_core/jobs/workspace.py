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
    for link in connection.execute(
        "SELECT l.* FROM basket_item_need_links l JOIN basket_items i ON i.id=l.basket_item_id "
        "JOIN baskets b ON b.id=i.basket_id WHERE b.job_id=?", (job_id,),
    ):
        links.setdefault(link["basket_item_id"], set()).add(link["requested_need_id"])
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
        selected = [item for item in part_options if item["selected"]]
        attached.update(item["id"] for item in part_options)
        # No exclusive replacement or kit allocation is inferred in this phase.
        ambiguous = len(selected) > 1 or any(
            item["shared"] or (need["job_asset_id"] is not None and item["job_asset_id"] != need["job_asset_id"])
            for item in part_options
        )
        part.update(options=part_options, selected_options=selected, asset=assets.get(need["job_asset_id"]),
                    ambiguous=ambiguous, suggested=None)
        part["status_label"] = (
            "Multiple selections · review quantities" if len(selected) > 1 else
            "Selected option" if selected else
            "Marked covered" if need["state"] == "SATISFIED" else
            f"{len(part_options)} options found" if part_options else "Need sourcing"
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
    needs_options = sum(not part["selected_options"] and part["state"] == "OPEN" for part in parts)
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
    return dict(job=job, operational_snapshot=snapshot, basket=basket, revision=revision,
                work_editable=editable, parts=parts, other_options=unassigned,
                selected_options=selected, outstanding=outstanding, primary_action=action,
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
