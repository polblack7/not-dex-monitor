from decimal import Decimal

from web3 import Web3

from not_dex_monitor.dex import create_quoters
from not_dex_monitor.dex.base import QuoteResult
from not_dex_monitor.quote_math import to_wei
from not_dex_monitor.tokens import parse_pair


def test_adapters_interface() -> None:
    w3 = Web3(Web3.HTTPProvider("http://localhost:8545"))
    adapters = create_quoters(w3)
    pair = parse_pair("WETH/USDC")
    amount_in = to_wei(Decimal("1"), pair.base.decimals)

    for adapter in adapters.values():
        supported = adapter.supports_pair(pair)
        assert isinstance(supported, bool)
        result = adapter.quote_exact_in(pair.base, pair.quote, amount_in)
        assert isinstance(result, QuoteResult)
