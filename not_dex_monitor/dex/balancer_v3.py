from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from web3 import Web3

from .abi import load_abi
from .addresses import BALANCER_V3_POOL_ADDRESSES, MAINNET_ADDRESSES, ZERO_ADDRESS
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class BalancerV3Adapter(BaseDexAdapter):
    """Balancer V3 price quotes via Router.querySwapSingleTokenExactIn.

    Unlike V2, V3 uses pool address directly (no pool ID), no asset sorting
    required, and the Router handles the query interface cleanly.
    """

    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="Balancer V3", gas_estimate=250_000)
        self.router_address = MAINNET_ADDRESSES.balancer_v3.router
        self.router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.router_address),
            abi=load_abi("balancer_v3", "router"),
        )

    def _supports_pair(self, pair: TokenPair) -> bool:
        return bool(self._pool_addresses_for_pair(pair.base.symbol, pair.quote.symbol))

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        pool_addresses = self._pool_addresses_for_pair(token_in.symbol, token_out.symbol)
        if not pool_addresses:
            return self._error_result(token_in, token_out, amount_in_wei, error="unsupported_pair")

        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)

        best_amount: Optional[int] = None
        best_pool: Optional[str] = None

        for pool_address in pool_addresses:
            pool_addr = self.w3.to_checksum_address(pool_address)
            try:
                amount_out = self.router.functions.querySwapSingleTokenExactIn(
                    pool_addr,
                    token_in_addr,
                    token_out_addr,
                    amount_in_wei,
                    ZERO_ADDRESS,
                    b"",
                ).call()
            except Exception:  # noqa: BLE001
                continue

            if not amount_out or amount_out <= 0:
                continue
            if best_amount is None or amount_out > best_amount:
                best_amount = amount_out
                best_pool = pool_address

        if best_amount is None:
            return self._error_result(token_in, token_out, amount_in_wei, error="quote_failed")

        return QuoteResult(
            amount_out_wei=best_amount,
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=None,
            diagnostics={"router": self.router_address, "pool": best_pool},
        )

    def _pool_addresses_for_pair(self, symbol_in: str, symbol_out: str) -> List[str]:
        key = (symbol_in.upper(), symbol_out.upper())
        alt_key = (symbol_out.upper(), symbol_in.upper())
        pools = BALANCER_V3_POOL_ADDRESSES.get(key) or BALANCER_V3_POOL_ADDRESSES.get(alt_key) or []
        return list(pools)
