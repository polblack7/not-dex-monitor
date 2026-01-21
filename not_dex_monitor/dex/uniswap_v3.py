from __future__ import annotations

from typing import Iterable, Optional, Tuple

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


DEFAULT_FEE_TIERS = (500, 3000, 10000)


class UniswapV3Adapter(BaseDexAdapter):
    def __init__(self, w3: Web3, *, fee_tiers: Iterable[int] = DEFAULT_FEE_TIERS) -> None:
        super().__init__(w3, name="Uniswap V3", gas_estimate=220_000)
        self.quoter_address = MAINNET_ADDRESSES.uniswap_v3.quoter_v2
        self.factory_address = MAINNET_ADDRESSES.uniswap_v3.factory
        self.quoter = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.quoter_address),
            abi=load_abi("uniswap_v3", "quoter_v2"),
        )
        self.factory = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.factory_address),
            abi=load_abi("uniswap_v3", "factory"),
        )
        self._fee_tiers = tuple(int(fee) for fee in fee_tiers)

    def _supports_pair(self, pair: TokenPair) -> bool:
        token_a = self.w3.to_checksum_address(pair.base.address)
        token_b = self.w3.to_checksum_address(pair.quote.address)
        for fee in self._fee_tiers:
            pool = self.factory.functions.getPool(token_a, token_b, fee).call()
            if pool and pool != "0x0000000000000000000000000000000000000000":
                return True
        return False

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        if not self.supports_pair(TokenPair(base=token_in, quote=token_out, raw="")):
            return self._error_result(token_in, token_out, amount_in_wei, error="unsupported_pair")

        token_in_addr = self.w3.to_checksum_address(token_in.address)
        token_out_addr = self.w3.to_checksum_address(token_out.address)
        best_amount: Optional[int] = None
        best_fee: Optional[int] = None
        best_gas: Optional[int] = None

        for fee in self._fee_tiers:
            params = (token_in_addr, token_out_addr, fee, amount_in_wei, 0)
            try:
                result = self.quoter.functions.quoteExactInputSingle(params).call()
            except Exception:  # noqa: BLE001
                continue
            amount_out, gas_estimate = _parse_quoter_result(result)
            if amount_out <= 0:
                continue
            if best_amount is None or amount_out > best_amount:
                best_amount = amount_out
                best_fee = fee
                best_gas = gas_estimate

        if best_amount is None:
            return self._error_result(token_in, token_out, amount_in_wei, error="quote_failed")

        return QuoteResult(
            amount_out_wei=best_amount,
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=best_gas or self.gas_estimate,
            error=None,
            diagnostics={
                "fee": best_fee,
                "quoter": self.quoter_address,
                "factory": self.factory_address,
            },
        )


def _parse_quoter_result(result: object) -> Tuple[int, Optional[int]]:
    if isinstance(result, (list, tuple)):
        amount_out = int(result[0])
        gas_estimate = int(result[3]) if len(result) > 3 else None
        return amount_out, gas_estimate
    return int(result), None
