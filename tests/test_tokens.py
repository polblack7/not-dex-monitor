import pytest

from not_dex_monitor.tokens import parse_pair, get_token


def test_parse_pair_eth_usdt() -> None:
    pair = parse_pair("ETH/USDT")
    assert pair.base.symbol == "WETH"
    assert pair.quote.symbol == "USDT"


def test_parse_pair_case_insensitive() -> None:
    pair = parse_pair("wbtc/eth")
    assert pair.base.symbol == "WBTC"
    assert pair.quote.symbol == "WETH"


def test_unknown_token() -> None:
    with pytest.raises(ValueError):
        get_token("FOO")
