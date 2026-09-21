from contextlib import closing
from fastapi import APIRouter, HTTPException
from legacy_app import get_connection
from plg_core.sales.models import ConversionRequest, InvoicePaymentRequest, InvoicePaymentReversalRequest, InvoiceVoidRequest, QuoteStatusUpdate
from plg_core.sales.service import get_invoice, get_quote, list_invoices, list_quotes, record_invoice_payment, reverse_invoice_payment, update_quote_status, void_invoice

router = APIRouter(prefix="/api/v1/sales", tags=["alpha12-13-sales"])

@router.get("/quotes")
def quotes(limit: int = 100):
    return {"items": list_quotes(limit)}

@router.get("/quotes/{quote_id}")
def quote_detail(quote_id: int):
    return get_quote(quote_id)

@router.post("/quotes/{quote_id}/status")
def quote_status(quote_id: int, payload: QuoteStatusUpdate):
    return update_quote_status(quote_id, payload.status, payload.notes)

@router.post("/quotes/{quote_id}/convert")
def convert_quote(quote_id: int, payload: ConversionRequest | None = None):
    quote = get_quote(quote_id)
    if quote.get("invoice_id"):
        return {"created": False, "invoice": get_invoice(int(quote["invoice_id"]))}
    status = str(quote.get("status") or "").upper()
    force = bool(payload.force) if payload else False
    if force:
        raise HTTPException(status_code=409, detail="Forced invoice conversion is disabled by lifecycle safety rules.")
    if status not in {"APPROVED", "ACCEPTED", "CONFIRMED"}:
        raise HTTPException(status_code=409, detail="Quote must be approved before API conversion.")
    from legacy_app import convert_quote_to_invoice
    convert_quote_to_invoice(quote_id)
    with closing(get_connection()) as connection:
        invoice = connection.execute(
            "SELECT id FROM invoices WHERE quote_id=?",
            (quote_id,),
        ).fetchone()
        if invoice is None:
            raise HTTPException(status_code=500, detail="Conversion did not create an invoice.")
    return {"created": True, "invoice": get_invoice(int(invoice["id"]))}

@router.get("/invoices")
def invoices(limit: int = 100):
    return {"items": list_invoices(limit)}

@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: int):
    return get_invoice(invoice_id)

@router.post("/invoices/{invoice_id}/payments")
def invoice_payment(
    invoice_id: int,
    payload: InvoicePaymentRequest,
):
    return record_invoice_payment(
        invoice_id=invoice_id,
        amount=payload.amount,
        payment_method=payload.payment_method,
        reference=payload.reference,
        payment_date=payload.payment_date,
    )

@router.post(
    "/invoices/{invoice_id}/payments/{payment_id}/reverse"
)
def invoice_payment_reversal(
    invoice_id: int,
    payment_id: int,
    payload: InvoicePaymentReversalRequest,
):
    return reverse_invoice_payment(
        invoice_id=invoice_id,
        payment_id=payment_id,
        amount=payload.amount,
        reason=payload.reason,
        reversal_date=payload.reversal_date,
    )

@router.post("/invoices/{invoice_id}/void")
def invoice_void(invoice_id: int, payload: InvoiceVoidRequest):
    return void_invoice(invoice_id, payload.reason)
