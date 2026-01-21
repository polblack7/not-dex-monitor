from __future__ import annotations

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


class UniswapV2LikeAdapter(BaseDexAdapter):
    def __init__(
        self,
        w3: Web3,
        *,
        name: str,
        router_address: str,
        factory_address: str,
        venue: str,
        gas_estimate: int = 180_000,
    ) -> None:
        super().__init__(w3, name=name, gas_estimate=gas_estimate)
        self.router_address = router_address
        self.factory_address = factory_address
        self.router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(router_address),
            abi=load_abi(venue, "router02"),
        )
        self.factory = self.w3.eth.contract(
            address=self.w3.to_checksum_address(factory_address),
            abi=load_abi(venue, "factory"),
        )

    def _supports_pair(self, pair: TokenPair) -> bool:
        token_a = self.w3.to_checksum_address(pair.base.address)
        token_b = self.w3.to_checksum_address(pair.quote.address)
        pair_address = self.factory.functions.getPair(token_a, token_b).call()
        return pair_address is not None and pair_address != "0x0000000000000000000000000000000000000000"

    def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
        if not self.supports_pair(TokenPair(base=token_in, quote=token_out, raw="")):
            return self._error_result(
                token_in,
                token_out,
                amount_in_wei,
                error="unsupported_pair",
            )
        path = [
            self.w3.to_checksum_address(token_in.address),
            self.w3.to_checksum_address(token_out.address),
        ]
        amounts = self.router.functions.getAmountsOut(amount_in_wei, path).call()
        amount_out = int(amounts[-1])
        return QuoteResult(
            amount_out_wei=amount_out,
            venue=self.name,
            route=[token_in.symbol, token_out.symbol],
            gas_estimate=self.gas_estimate,
            error=None,
            diagnostics={"router": self.router_address, "factory": self.factory_address},
        )


class UniswapV2Adapter(UniswapV2LikeAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(
            w3,
            name="Uniswap V2",
            router_address=MAINNET_ADDRESSES.uniswap_v2.router,
            factory_address=MAINNET_ADDRESSES.uniswap_v2.factory,
            venue="uniswap_v2",
            gas_estimate=180_000,
        )
