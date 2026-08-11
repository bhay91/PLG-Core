from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from .service import (
    create_manual_research_result,
    create_requested_need,
    save_shipping_data,
    set_quote_candidate,
    update_requested_need,
)


router = APIRouter(tags=["machine-research"])


def _workspace(job_id: int, asset_id: int | None, anchor: str = "machine-workspace"):
    query = f"?asset_id={asset_id}" if asset_id is not None else ""
    return f"/jobs/{job_id}/basket{query}#{anchor}"


@router.post("/jobs/{job_id}/needs")
def add_need(
    job_id: int, wording: Annotated[str, Form()], job_asset_id: Annotated[int | None, Form()] = None,
    notes: Annotated[str, Form()] = "", expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    create_requested_need(job_id, job_asset_id=job_asset_id, wording=wording, notes=notes,
                          expected_revision_id=expected_revision_id, expected_version=expected_version)
    return RedirectResponse(_workspace(job_id, job_asset_id, "customer-needs"), status_code=303)


@router.post("/jobs/{job_id}/needs/{need_id}")
def edit_need(
    job_id: int, need_id: int, wording: Annotated[str, Form()], state: Annotated[str, Form()] = "OPEN",
    resolution: Annotated[str, Form()] = "", job_asset_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    update_requested_need(job_id, need_id, wording=wording, state=state, resolution=resolution,
                          expected_revision_id=expected_revision_id, expected_version=expected_version)
    return RedirectResponse(_workspace(job_id, job_asset_id, "customer-needs"), status_code=303)


@router.post("/jobs/{job_id}/research-results/manual")
def add_manual_result(
    job_id: int, description: Annotated[str, Form()], job_asset_id: Annotated[int | None, Form()] = None,
    requested_need_id: Annotated[int | None, Form()] = None,
    manufacturer_part_number: Annotated[str, Form()] = "", quantity: Annotated[int, Form()] = 1,
    supplier_name: Annotated[str, Form()] = "", supplier_part_number: Annotated[str, Form()] = "",
    supplier_unit_cost: Annotated[float | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    create_manual_research_result(
        job_id, job_asset_id=job_asset_id, requested_need_id=requested_need_id,
        description=description, manufacturer_part_number=manufacturer_part_number,
        quantity=quantity, supplier_name=supplier_name, supplier_part_number=supplier_part_number,
        supplier_unit_cost=supplier_unit_cost, expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(_workspace(job_id, job_asset_id, "research-results"), status_code=303)


@router.post("/jobs/{job_id}/research-results/{item_id}/candidate")
def candidate_result(
    job_id: int, item_id: int, candidate: Annotated[int, Form()] = 1,
    requested_need_ids: Annotated[list[int] | None, Form()] = None,
    job_asset_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    set_quote_candidate(job_id, item_id, candidate=bool(candidate),
                        requested_need_ids=requested_need_ids or [],
                        expected_revision_id=expected_revision_id, expected_version=expected_version)
    return RedirectResponse(_workspace(job_id, job_asset_id, "research-results"), status_code=303)


@router.post("/jobs/{job_id}/research-results/{item_id}/shipping")
def shipping_data(
    job_id: int, item_id: int, quality: Annotated[str, Form()] = "MANUAL",
    unit_weight: Annotated[float | None, Form()] = None, weight_unit: Annotated[str, Form()] = "lb",
    length: Annotated[float | None, Form()] = None, width: Annotated[float | None, Form()] = None,
    height: Annotated[float | None, Form()] = None, dimension_unit: Annotated[str, Form()] = "in",
    provenance: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
    job_asset_id: Annotated[int | None, Form()] = None,
):
    save_shipping_data(job_id, item_id, quality=quality, unit_weight=unit_weight,
                       weight_unit=weight_unit, length=length, width=width, height=height,
                       dimension_unit=dimension_unit, provenance=provenance, notes=notes)
    return RedirectResponse(_workspace(job_id, job_asset_id, "research-results"), status_code=303)
