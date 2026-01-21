from decimal import Decimal

import pytest

from not_dex_monitor.models import compute_expected_profit_pct


def test_compute_expected_profit_pct_positive() -> None:
    pct = compute_expected_profit_pct(
        buy_price=Decimal("100"),
        sell_price=Decimal("102"),
        base_amount=Decimal("1"),
        fees_in_quote=Decimal("0.1"),
    )
    assert float(pct) == pytest.approx(1.9, abs=1e-6)


def test_compute_expected_profit_pct_negative() -> None:
    pct = compute_expected_profit_pct(
        buy_price=Decimal("100"),
        sell_price=Decimal("99"),
        base_amount=Decimal("1"),
        fees_in_quote=Decimal("0.2"),
    )
    assert float(pct) < 0
