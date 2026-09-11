from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from .service import (
    add_job_asset, archive_job_asset, assign_basket_item_asset,
    edit_job_asset, set_primary_job_asset,
)

router = APIRouter(tags=["job-assets"])


@router.post("/jobs/{job_id}/assets")
def add_asset_route(
    job_id: int,
    manufacturer: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
    vin_pin_serial: Annotated[str, Form()] = "",
    asset_type: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    machine_id: Annotated[int | None, Form()] = None,
    make_primary: Annotated[int, Form()] = 0,
):
    add_job_asset(
        job_id, manufacturer=manufacturer, model=model, year=year,
        vin_pin_serial=vin_pin_serial, asset_type=asset_type, name=name,
        notes=notes, machine_id=machine_id, make_primary=bool(make_primary),
    )
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#job-assets", status_code=303)


@router.post("/jobs/{job_id}/assets/{asset_id}/edit")
def edit_asset_route(
    job_id: int, asset_id: int,
    manufacturer: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    year: Annotated[str, Form()] = "",
    vin_pin_serial: Annotated[str, Form()] = "",
    asset_type: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    edit_job_asset(
        job_id, asset_id, manufacturer=manufacturer, model=model, year=year,
        vin_pin_serial=vin_pin_serial, asset_type=asset_type, name=name, notes=notes,
    )
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#job-assets", status_code=303)


@router.post("/jobs/{job_id}/assets/{asset_id}/primary")
def primary_asset_route(job_id: int, asset_id: int):
    set_primary_job_asset(job_id, asset_id)
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#job-assets", status_code=303)


@router.post("/jobs/{job_id}/assets/{asset_id}/archive")
def archive_asset_route(
    job_id: int, asset_id: int, reason: Annotated[str, Form()],
):
    archive_job_asset(job_id, asset_id, reason=reason)
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#job-assets", status_code=303)


@router.post("/jobs/{job_id}/basket/items/{item_id}/asset")
def assign_asset_route(
    job_id: int, item_id: int,
    job_asset_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    assign_basket_item_asset(
        job_id, item_id, job_asset_id,
        expected_revision_id=expected_revision_id, expected_version=expected_version,
    )
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#parts-ready", status_code=303)
