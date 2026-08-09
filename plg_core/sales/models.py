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
