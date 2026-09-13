from __future__ import annotations

import math


def recommended_markup_percent(cost: float) -> float:
    if cost <= 50:
        return 40.0
    if cost <= 200:
        return 30.0
    if cost <= 500:
        return 25.0
    return 20.0


def customer_unit_price(
    cost: float,
    markup_percent: float | None = None,
) -> float:
    if markup_percent is not None:
        return round(cost * (1 + float(markup_percent) / 100), 2)

    markup = recommended_markup_percent(cost) / 100
    return float(math.ceil(cost * (1 + markup)))


def effective_customer_unit_price(
    cost: float,
    markup_percent: float | None = None,
    customer_unit_price_override: float | None = None,
) -> float:
    if customer_unit_price_override is not None:
        return round(float(customer_unit_price_override), 2)

    return customer_unit_price(cost, markup_percent)


def pricing_assessment(
    cost: float,
    markup_percent: float | None = None,
    customer_unit_price_override: float | None = None,
) -> dict[str, float | str | bool]:
    cost = float(cost or 0)
    recommended_markup = recommended_markup_percent(cost)
    recommended_price = customer_unit_price(cost)

    effective_markup = (
        recommended_markup
        if markup_percent is None
        else float(markup_percent)
    )
    current_price = effective_customer_unit_price(
        cost,
        None if markup_percent is None else effective_markup,
        customer_unit_price_override,
    )
    # Keep the automatic reference distinct from the effective price when an
    # operator override is active. Both values use the stored markup.
    automatic_unit_price = customer_unit_price(cost, effective_markup)
    unit_profit = round(current_price - cost, 2)
    actual_markup_percent = (
        round(((current_price - cost) / cost) * 100, 2)
        if cost > 0
        else None
    )

    if cost <= 0:
        status = "MISSING_COST"
    elif current_price < cost:
        status = "BELOW_COST"
    elif current_price == cost:
        status = "AT_COST"
    elif (
        customer_unit_price_override is not None
        and current_price < recommended_price
    ):
        status = "BELOW_RECOMMENDED"
    elif (
        customer_unit_price_override is None
        and effective_markup < recommended_markup
    ):
        status = "BELOW_RECOMMENDED"
    else:
        status = "ON_TARGET"

    return {
        "recommended_markup_percent": recommended_markup,
        "recommended_unit_price": recommended_price,
        "automatic_unit_price": automatic_unit_price,
        "current_markup_percent": effective_markup,
        "actual_markup_percent": actual_markup_percent,
        "current_unit_price": current_price,
        "unit_profit": unit_profit,
        "status": status,
        "below_recommended": status == "BELOW_RECOMMENDED",
    }
