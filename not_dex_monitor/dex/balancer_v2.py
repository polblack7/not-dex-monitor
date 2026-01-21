from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from web3 import Web3

from .abi import load_abi
from .addresses import BALANCER_POOL_ADDRESSES, MAINNET_ADDRESSES, ZERO_ADDRESS
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class BalancerV2Adapter(BaseDexAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="Balancer V2", gas_estimate=240_000)
        self.vault_address = MAINNET_ADDRESSES.balancer_v2.vault
        self.vault = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.vault_address),
            abi=load_abi("balancer_v2", "vault"),
        )
        self.weighted_pool_abi = load_abi("balancer_v2", "weighted_pool")
        self.stable_pool_abi = load_abi("balancer_v2", "stable_pool")
        self._pool_ids: Dict[str, object] = {}

    def _supports_pair(self, pair: TokenPair) -> bool:
        return bool(self._pool_addresses_for_pair(pair.base.symbol, pair.quote.symbol))

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        pool_addresses = self._pool_addresses_for_pair(token_in.symbol, token_out.symbol)
        if not pool_addresses:
            return self._error_result(token_in, token_out, amount_in_wei, error="unsupported_pair")

        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)
        assets = [token_in_addr, token_out_addr]
        funds = (ZERO_ADDRESS, False, ZERO_ADDRESS, False)

        best_amount: Optional[int] = None
        best_pool: Optional[str] = None
        for pool_address in pool_addresses:
            pool_id = self._get_pool_id(pool_address)
            swaps = [(pool_id, 0, 1, amount_in_wei, b"")]
            try:
                deltas = self.vault.functions.queryBatchSwap(0, swaps, assets, funds).call()
            except Exception:  # noqa: BLE001
                continue
            if not deltas or len(deltas) < 2:
                continue
            out_delta = int(deltas[1])
            amount_out = -out_delta if out_delta < 0 else 0
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
            diagnostics={
                "vault": self.vault_address,
                "pool": best_pool,
            },
        )

    def _pool_addresses_for_pair(self, symbol_in: str, symbol_out: str) -> List[str]:
        key = (symbol_in.upper(), symbol_out.upper())
        alt_key = (symbol_out.upper(), symbol_in.upper())
        pools = BALANCER_POOL_ADDRESSES.get(key) or BALANCER_POOL_ADDRESSES.get(alt_key) or []
        return list(pools)

    def _get_pool_id(self, pool_address: str) -> object:
        if pool_address in self._pool_ids:
            return self._pool_ids[pool_address]
        pool_id = self._call_get_pool_id(pool_address, self.weighted_pool_abi)
        if pool_id is None:
            pool_id = self._call_get_pool_id(pool_address, self.stable_pool_abi)
        if pool_id is None:
            raise RuntimeError("Balancer poolId lookup failed")
        self._pool_ids[pool_address] = pool_id
        return pool_id

    def _call_get_pool_id(self, pool_address: str, abi: Iterable[dict]) -> Optional[object]:
        try:
            pool = self.w3.eth.contract(address=self.w3.to_checksum_address(pool_address), abi=abi)
            return pool.functions.getPoolId().call()
        except Exception:  # noqa: BLE001
            return None
