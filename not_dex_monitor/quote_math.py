from __future__ import annotations

from decimal import Decimal


def to_wei(amount: Decimal, decimals: int) -> int:
    scale = Decimal(10) ** decimals
    return int((amount * scale).to_integral_value())


def price_from_amounts(
    amount_in: int,
    amount_out: int,
    base_decimals: int,
    quote_decimals: int,
) -> Decimal:
    amount_in_dec = Decimal(amount_in) / Decimal(10**base_decimals)
    amount_out_dec = Decimal(amount_out) / Decimal(10**quote_decimals)
    if amount_in_dec == 0:
        return Decimal("0")
    return amount_out_dec / amount_in_dec
