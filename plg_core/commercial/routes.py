from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from .service import create_selective_draft_quote, split_issued_quote, update_draft_bill_to

router=APIRouter(tags=["commercial-quotes"])


@router.post("/jobs/{job_id}/quote-builder")
def build_quote_route(
    job_id:int,
    basket_item_ids:Annotated[list[int],Form()],
    bill_to_kind:Annotated[str,Form()]="CONTACT",
    bill_to_name:Annotated[str,Form()]="",
    bill_to_company:Annotated[str,Form()]="",
    bill_to_address:Annotated[str,Form()]="",
    expected_revision_id:Annotated[int,Form()]=0,
    expected_version:Annotated[int,Form()]=0,
):
    quote=create_selective_draft_quote(job_id,basket_item_ids=basket_item_ids,bill_to_kind=bill_to_kind,bill_to_values={"name":bill_to_name,"company":bill_to_company,"address":bill_to_address},expected_revision_id=expected_revision_id,expected_version=expected_version)
    return RedirectResponse(f"/quotes/{quote['id']}/documents",status_code=303)


@router.post("/quotes/{quote_id}/bill-to")
def update_bill_to_route(
    quote_id:int,bill_to_kind:Annotated[str,Form()],
    bill_to_name:Annotated[str,Form()]="",bill_to_company:Annotated[str,Form()]="",
    bill_to_address:Annotated[str,Form()]="",
):
    update_draft_bill_to(quote_id,kind=bill_to_kind,values={"name":bill_to_name,"company":bill_to_company,"address":bill_to_address})
    return RedirectResponse(f"/quotes/{quote_id}/documents",status_code=303)


@router.post("/quotes/{quote_id}/split")
def split_quote_route(
    quote_id:int,reason:Annotated[str,Form()],
    contact_item_ids:Annotated[list[int],Form()]=[],
    company_item_ids:Annotated[list[int],Form()]=[],
):
    groups=[]
    if contact_item_ids: groups.append({"quote_item_ids":contact_item_ids,"bill_to_kind":"CONTACT"})
    if company_item_ids: groups.append({"quote_item_ids":company_item_ids,"bill_to_kind":"COMPANY"})
    successors=split_issued_quote(quote_id,groups=groups,reason=reason)
    return RedirectResponse(f"/quotes/{successors[0]['id']}/documents",status_code=303)
