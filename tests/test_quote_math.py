from decimal import Decimal

import pytest

from not_dex_monitor.quote_math import price_from_amounts, to_wei


def test_to_wei() -> None:
    assert to_wei(Decimal("1"), 18) == 10**18
    assert to_wei(Decimal("0.5"), 6) == 500_000


def test_price_from_amounts() -> None:
    amount_in = 10**18
    amount_out = 2_000 * 10**6
    price = price_from_amounts(amount_in, amount_out, 18, 6)
    assert float(price) == pytest.approx(2000.0)
