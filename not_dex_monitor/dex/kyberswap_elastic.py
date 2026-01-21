from __future__ import annotations

from typing import Iterable, Optional, Tuple

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


DEFAULT_FEE_TIERS = (8, 40, 100, 300, 1000)


class KyberSwapElasticAdapter(BaseDexAdapter):
    def __init__(self, w3: Web3, *, fee_tiers: Iterable[int] = DEFAULT_FEE_TIERS) -> None:
        super().__init__(w3, name="KyberSwap Elastic", gas_estimate=230_000)
        self.quoter_address = MAINNET_ADDRESSES.kyberswap_elastic.quoter
        self.factory_address = MAINNET_ADDRESSES.kyberswap_elastic.factory
        self.quoter = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.quoter_address),
            abi=load_abi("kyberswap_elastic", "quoter"),
        )
        self.factory = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.factory_address),
            abi=load_abi("kyberswap_elastic", "factory"),
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

        for fee in self._fee_tiers:
            try:
                result = self._call_quoter(token_in_addr, token_out_addr, fee, amount_in_wei)
            except Exception:  # noqa: BLE001
                continue
            amount_out = _parse_quoter_amount(result)
            if amount_out <= 0:
                continue
            if best_amount is None or amount_out > best_amount:
                best_amount = amount_out
                best_fee = fee

        if best_amount is None:
            return self._error_result(token_in, token_out, amount_in_wei, error="quote_failed")

        return QuoteResult(
            amount_out_wei=best_amount,
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=None,
            diagnostics={"fee": best_fee, "quoter": self.quoter_address},
        )

    def _call_quoter(self, token_in: str, token_out: str, fee: int, amount_in_wei: int):
        try:
            return self.quoter.functions.quoteExactInputSingle(
                token_in,
                token_out,
                fee,
                amount_in_wei,
                0,
            ).call()
        except Exception:  # noqa: BLE001
            params = (token_in, token_out, fee, amount_in_wei, 0)
            return self.quoter.functions.quoteExactInputSingle(params).call()


def _parse_quoter_amount(result: object) -> int:
    if isinstance(result, (list, tuple)):
        return int(result[0])
    return int(result)
