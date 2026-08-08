from __future__ import annotations

import math


def customer_unit_price(
    cost: float,
    markup_percent: float | None = None,
) -> float:
    if markup_percent is not None:
        return round(cost * (1 + float(markup_percent) / 100), 2)

    if cost <= 50:
        markup = 0.40
    elif cost <= 200:
        markup = 0.30
    elif cost <= 500:
        markup = 0.25
    else:
        markup = 0.20

    return float(math.ceil(cost * (1 + markup)))
