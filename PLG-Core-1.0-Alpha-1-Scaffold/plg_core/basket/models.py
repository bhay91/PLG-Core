from dataclasses import dataclass

@dataclass
class BasketItem:
    description: str
    quantity: int
    supplier_cost: float
    source: str
