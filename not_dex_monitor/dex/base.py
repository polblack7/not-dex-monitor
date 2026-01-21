from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3

from ..tokens import Token, TokenPair
from ..util.logging import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class QuoteResult:
    amount_out_wei: Optional[int]
    venue: str
    route: List[str]
    gas_estimate: Optional[int]
    error: Optional[str]
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.amount_out_wei is not None


class BaseDexAdapter:
    name: str
    gas_estimate: int

    def __init__(self, w3: Web3, *, name: str, gas_estimate: int = 180_000) -> None:
        self.w3 = w3
        self.name = name
        self.gas_estimate = gas_estimate
        self._supports_cache: Dict[Tuple[str, str], bool] = {}

    def supports_pair(self, pair: TokenPair) -> bool:
        key = (pair.base.address.lower(), pair.quote.address.lower())
        if key in self._supports_cache:
            return self._supports_cache[key]
        try:
            supported = self._supports_pair(pair)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s supports_pair failed: %s", self.name, exc)
            supported = False
        self._supports_cache[key] = supported
        return supported

    def quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        try:
            return self._quote_exact_in(token_in, token_out, amount_in_wei)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                token_in,
                token_out,
                amount_in_wei,
                error=str(exc),
                diagnostics={"exception": str(exc)},
            )

    async def quote_exact_in_async(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        return await asyncio.to_thread(self.quote_exact_in, token_in, token_out, amount_in_wei)

    def _error_result(
        self,
        token_in: Token,
        token_out: Token,
        amount_in_wei: int,
        *,
        error: str,
        diagnostics: Optional[Dict[str, Any]] = None,
        route: Optional[List[str]] = None,
    ) -> QuoteResult:
        details = diagnostics or {}
        details.setdefault("amount_in_wei", amount_in_wei)
        return QuoteResult(
            amount_out_wei=None,
            venue=self.name,
            route=route or [token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=error,
            diagnostics=details,
        )

    def _supports_pair(self, pair: TokenPair) -> bool:
        raise NotImplementedError

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        raise NotImplementedError
