from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from .service import start_part_verification

router=APIRouter(tags=["verification"])


@router.post("/jobs/{job_id}/basket/items/{item_id}/verify")
def verify_part_route(
    job_id:int,item_id:int,
    connector_profile_id:Annotated[int,Form()],
    expected_revision_id:Annotated[int|None,Form()]=None,
    expected_version:Annotated[int|None,Form()]=None,
):
    start_part_verification(
        job_id,item_id,connector_profile_id,
        expected_revision_id=expected_revision_id,expected_version=expected_version,
    )
    return RedirectResponse(f"/jobs/{job_id}/basket#parts-ready",status_code=303)
