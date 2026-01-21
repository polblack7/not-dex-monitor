from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field


class Settings(BaseModel):
    min_profit_pct: float = Field(default=0.3)
    loan_limit: float = Field(default=3.0)
    dex_list: List[str] = Field(default_factory=list)
    pairs: List[str] = Field(default_factory=list)
    scan_frequency_sec: int = Field(default=15)
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class PriceQuote:
    dex: str
    amount_in: int
    amount_out: int
    price: Decimal


@dataclass(frozen=True)
class Opportunity:
    pair: str
    buy_dex: str
    sell_dex: str
    expected_profit_pct: float
    liquidity_score: float
    gas_price_gwei: float
    route: List[str]
    fees_quote: float


def compute_expected_profit_pct(
    *,
    buy_price: Decimal,
    sell_price: Decimal,
    base_amount: Decimal,
    fees_in_quote: Decimal,
) -> Decimal:
    buy_cost = buy_price * base_amount
    sell_return = sell_price * base_amount
    if buy_cost <= 0:
        return Decimal("0")
    return (sell_return - buy_cost - fees_in_quote) / buy_cost * Decimal("100")
