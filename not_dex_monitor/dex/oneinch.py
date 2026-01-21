from __future__ import annotations

from typing import Optional

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class OneInchAdapter(BaseDexAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="1inch", gas_estimate=260_000)
        self.router_address = MAINNET_ADDRESSES.oneinch.aggregation_router_v5
        self.router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.router_address),
            abi=load_abi("oneinch", "aggregation_router_v5"),
        )
        self.legacy_router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.router_address),
            abi=load_abi("oneinch", "onesplit"),
        )

    def _supports_pair(self, pair: TokenPair) -> bool:
        return True

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)
        last_error: Optional[str] = None

        try:
            result = self.router.functions.getRate(token_in_addr, token_out_addr, amount_in_wei, 0).call()
            amount_out = int(result[0])
            gas_estimate = int(result[1]) if len(result) > 1 else None
            return QuoteResult(
                amount_out_wei=amount_out,
                venue=self.name,
                route=[token_in.symbol, token_out.symbol],
                gas_estimate=gas_estimate or self.gas_estimate,
                error=None,
                diagnostics={"method": "getRate", "router": self.router_address},
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

        try:
            result = self.legacy_router.functions.getExpectedReturn(
                token_in_addr,
                token_out_addr,
                amount_in_wei,
                50,
                0,
            ).call()
            amount_out = int(result[0])
            return QuoteResult(
                amount_out_wei=amount_out,
                venue=self.name,
                route=[token_in.symbol, token_out.symbol],
                gas_estimate=self.gas_estimate,
                error=None,
                diagnostics={"method": "getExpectedReturn", "router": self.router_address},
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

        return self._error_result(
            token_in,
            token_out,
            amount_in_wei,
            error="unsupported_without_offchain",
            diagnostics={"router": self.router_address, "last_error": last_error or "unknown"},
        )
