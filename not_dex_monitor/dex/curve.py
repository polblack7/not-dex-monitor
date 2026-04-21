from __future__ import annotations

from typing import Optional

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES, ZERO_ADDRESS
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class CurveAdapter(BaseDexAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(w3, name="Curve", gas_estimate=220_000)
        self.address_provider_address = MAINNET_ADDRESSES.curve.address_provider
        self.registry_address = MAINNET_ADDRESSES.curve.registry
        self.address_provider = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.address_provider_address),
            abi=load_abi("curve", "address_provider"),
        )
        self.registry = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.registry_address),
            abi=load_abi("curve", "registry"),
        )
        self.pool_abi = load_abi("curve", "pool")
        self.crypto_pool_abi = load_abi("curve", "crypto_pool")

    def _supports_pair(self, pair: TokenPair) -> bool:
        pool_address = self._find_pool(pair.base.address, pair.quote.address)
        return pool_address is not None

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        pool_address = self._find_pool(token_in.address, token_out.address)
        if pool_address is None:
            return self._error_result(token_in, token_out, amount_in_wei, error="unsupported_pair")

        registry = self._refresh_registry()
        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)
        i, j, is_underlying = registry.functions.get_coin_indices(
            self.w3.to_checksum_address(pool_address),
            token_in_addr,
            token_out_addr,
        ).call()
        pool = self.w3.eth.contract(
            address=self.w3.to_checksum_address(pool_address),
            abi=self.pool_abi,
        )
        crypto_pool = self.w3.eth.contract(
            address=self.w3.to_checksum_address(pool_address),
            abi=self.crypto_pool_abi,
        )

        amount_out = None
        if bool(is_underlying):
            try:
                amount_out = pool.functions.get_dy_underlying(int(i), int(j), amount_in_wei).call()
            except Exception:  # noqa: BLE001
                amount_out = None
        if amount_out is None:
            try:
                amount_out = pool.functions.get_dy(int(i), int(j), amount_in_wei).call()
            except Exception:  # noqa: BLE001
                amount_out = None
        if amount_out is None:
            # CryptoSwap V2 pools use uint256 indices instead of int128
            amount_out = crypto_pool.functions.get_dy(int(i), int(j), amount_in_wei).call()

        return QuoteResult(
            amount_out_wei=int(amount_out),
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=None,
            diagnostics={
                "pool": pool_address,
                "index_in": int(i),
                "index_out": int(j),
                "underlying": bool(is_underlying),
            },
        )

    def _refresh_registry(self):
        # Prefer MetaRegistry (id=7) — it aggregates all Curve pools including newer ones.
        # Fall back to the old Main Registry (id=0 / get_registry()) for legacy pools.
        registry = None
        for getter in (
            lambda: self.address_provider.functions.get_address(7).call(),
            lambda: self.address_provider.functions.get_address(0).call(),
            lambda: self.address_provider.functions.get_registry().call(),
        ):
            try:
                addr = getter()
                if addr and addr != ZERO_ADDRESS:
                    registry = addr
                    break
            except Exception:  # noqa: BLE001
                continue

        if registry and registry.lower() != self.registry_address.lower():
            self.registry_address = registry
            self.registry = self.w3.eth.contract(
                address=self.w3.to_checksum_address(registry),
                abi=load_abi("curve", "registry"),
            )
        return self.registry

    def _find_pool(self, token_in: str, token_out: str) -> Optional[str]:
        registry = self._refresh_registry()
        token_in_addr = self.w3.to_checksum_address(token_in)
        token_out_addr = self.w3.to_checksum_address(token_out)
        pool_address: Optional[str] = None
        try:
            pool_address = registry.functions.find_pool_for_coins(token_in_addr, token_out_addr).call()
        except Exception:  # noqa: BLE001
            pool_address = None
        if not pool_address or pool_address == ZERO_ADDRESS:
            try:
                pool_address = registry.functions.find_pool_for_coins(token_in_addr, token_out_addr, 0).call()
            except Exception:  # noqa: BLE001
                pool_address = None
        if not pool_address or pool_address == ZERO_ADDRESS:
            return None
        return pool_address
