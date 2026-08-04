from __future__ import annotations

from fastapi import APIRouter

from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.service import (
    add_item,
    clear_basket,
    delete_item,
    get_basket,
    update_item,
)


router = APIRouter(prefix="/api/baskets", tags=["basket"])


@router.get("/{job_id}")
def read_basket(job_id: int):
    return get_basket(job_id)


@router.post("/{job_id}/items")
def create_basket_item(job_id: int, payload: BasketItemCreate):
    return add_item(job_id, payload)


@router.patch("/items/{item_id}")
def edit_basket_item(item_id: int, payload: BasketItemUpdate):
    return update_item(item_id, payload)


@router.delete("/items/{item_id}")
def remove_basket_item(item_id: int):
    return delete_item(item_id)


@router.delete("/{job_id}")
def remove_all_basket_items(job_id: int):
    return clear_basket(job_id)
