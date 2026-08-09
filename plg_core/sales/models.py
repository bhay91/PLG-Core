from typing import Literal
from pydantic import BaseModel, Field

QuoteStatus = Literal[
    "DRAFT", "SENT", "APPROVED", "REJECTED",
    "REVISION_REQUIRED", "CONVERTED"
]

class QuoteStatusUpdate(BaseModel):
    status: QuoteStatus
    notes: str = Field(default="", max_length=1000)

class ConversionRequest(BaseModel):
    force: bool = False

class InvoiceVoidRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)

class InvoicePaymentRequest(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str = Field(min_length=1, max_length=50)
    reference: str = Field(default="", max_length=200)
    payment_date: str = Field(default="", max_length=10)

class InvoicePaymentReversalRequest(BaseModel):
    amount: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    reversal_date: str = Field(default="", max_length=10)
