from __future__ import annotations

from typing import Optional

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair

# 1 ETH in wei — used as the reference amount for getRate()
_ONE_ETH = 10 ** 18


class OneInchAdapter(BaseDexAdapter):
    """On-chain price via 1inch OffchainOracle (getRate / getRateToEth).

    The AggregationRouterV5 does not expose price-query functions; all
    on-chain rate queries go through the OffchainOracle contract.
    """

    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="1inch", gas_estimate=260_000)
        self.router_address = MAINNET_ADDRESSES.oneinch.aggregation_router_v5
        self.oracle_address = MAINNET_ADDRESSES.oneinch.offchain_oracle
        self.oracle = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.oracle_address),
            abi=load_abi("oneinch", "offchain_oracle"),
        )

    def _supports_pair(self, pair: TokenPair) -> bool:
        return True

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)

        # getRate returns the price of token_in expressed in token_out units,
        # scaled to 1e18. We multiply by amount_in and rescale.
        rate: Optional[int] = None
        try:
            rate = int(self.oracle.functions.getRate(token_in_addr, token_out_addr, False).call())
        except Exception:
            try:
                rate = int(self.oracle.functions.getRate(token_in_addr, token_out_addr, True).call())
            except Exception:
                pass

        if rate is None or rate == 0:
            return self._error_result(
                token_in,
                token_out,
                amount_in_wei,
                error="oracle_rate_unavailable",
                diagnostics={"oracle": self.oracle_address},
            )

        # rate = (1 token_in expressed as token_out) * 1e18 / (10 ** token_out.decimals)
        # amount_out = amount_in_wei * rate / 1e18
        amount_out = amount_in_wei * rate // _ONE_ETH

        return QuoteResult(
            amount_out_wei=amount_out,
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=None,
            diagnostics={"method": "getRate", "oracle": self.oracle_address},
        )
