from __future__ import annotations

from web3 import Web3

from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class ZeroExAdapter(BaseDexAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="0x", gas_estimate=260_000)
        self.exchange_proxy_address = MAINNET_ADDRESSES.zeroex.exchange_proxy

    def _supports_pair(self, pair: TokenPair) -> bool:
        return True

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        return self._error_result(
            token_in,
            token_out,
            amount_in_wei,
            error="unsupported_without_offchain",
            diagnostics={
                "exchange_proxy": self.exchange_proxy_address,
                "reason": "0x pricing requires offchain RFQ or API sampling",
            },
        )
