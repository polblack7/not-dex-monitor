from __future__ import annotations

from typing import List, Optional

from web3 import Web3

from .abi import load_abi
from .addresses import DODO_V2_POOL_ADDRESSES, MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class DodoV2Adapter(BaseDexAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="DODO V2", gas_estimate=240_000)
        self.proxy_address = MAINNET_ADDRESSES.dodo_v2.proxy
        self.pool_abi = load_abi("dodo_v2", "pool")

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
            pool = self.w3.eth.contract(
                address=self.w3.to_checksum_address(pool_address),
                abi=self.pool_abi,
            )
            try:
                base_token = pool.functions.baseToken().call()
                quote_token = pool.functions.quoteToken().call()
            except Exception:  # noqa: BLE001
                continue

            try:
                if token_in_addr.lower() == str(base_token).lower() and token_out_addr.lower() == str(quote_token).lower():
                    amount_out = int(pool.functions.querySellBaseToken(amount_in_wei).call())
                elif token_in_addr.lower() == str(quote_token).lower() and token_out_addr.lower() == str(base_token).lower():
                    amount_out = int(pool.functions.querySellQuoteToken(amount_in_wei).call())
                else:
                    continue
            except Exception:  # noqa: BLE001
                continue

            if amount_out <= 0:
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
            diagnostics={"pool": best_pool, "proxy": self.proxy_address},
        )

    def _pool_addresses_for_pair(self, symbol_in: str, symbol_out: str) -> List[str]:
        key = (symbol_in.upper(), symbol_out.upper())
        alt_key = (symbol_out.upper(), symbol_in.upper())
        pools = DODO_V2_POOL_ADDRESSES.get(key) or DODO_V2_POOL_ADDRESSES.get(alt_key) or []
        return list(pools)
