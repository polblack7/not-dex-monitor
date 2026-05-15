from __future__ import annotations

from web3 import Web3

from .abi import load_abi
from .addresses import MAINNET_ADDRESSES
from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


_MIN_RESERVE_WEI = {
  "WETH": 10 * 10 ** 18,           # ≥ 10 WETH
  "WBTC": 10 ** 8,              # ≥ 1 WBTC
  "CBBTC": 10 ** 8,             # ≥ 1 cbBTC
  "WSTETH": 10 * 10 ** 18,          # ≥ 10 wstETH
  "USDC": 100_000 * 10 ** 6,         # ≥ 100k USDC
  "USDT": 100_000 * 10 ** 6,         # ≥ 100k USDT
  "DAI": 100_000 * 10 ** 18,         # ≥ 100k DAI
  "USDS": 100_000 * 10 ** 18,        # ≥ 100k USDS
  "USDE": 100_000 * 10 ** 18,        # ≥ 100k USDe
  "GHO": 100_000 * 10 ** 18,         # ≥ 100k GHO
  "FRAX": 100_000 * 10 ** 18,        # ≥ 100k FRAX
  "PEPE": 100_000_000_000 * 10 ** 18,    # ≥ 100B PEPE (~$1M)
}


def _min_reserve(token) -> int:
  return _MIN_RESERVE_WEI.get(token.symbol, 0)


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
    self._venue = venue
    self.router = self.w3.eth.contract(
      address=self.w3.to_checksum_address(router_address),
      abi=load_abi(venue, "router02"),
    )
    self.factory = self.w3.eth.contract(
      address=self.w3.to_checksum_address(factory_address),
      abi=load_abi(venue, "factory"),
    )
    self._pair_abi = load_abi(venue, "pair")

  def _supports_pair(self, pair: TokenPair) -> bool:
    token_a = self.w3.to_checksum_address(pair.base.address)
    token_b = self.w3.to_checksum_address(pair.quote.address)
    pair_address = self.factory.functions.getPair(token_a, token_b).call()
    if not pair_address or pair_address == "0x0000000000000000000000000000000000000000":
      return False
    # Reject dead/shallow pools -- quotes from them are dominated by slippage
    # and never yield real arbitrage. Threshold = 10x of the smallest
    # meaningful trade size for that token (1 WETH, 0.1 WBTC, 1000 USD).
    try:
      pool = self.w3.eth.contract(
        address=self.w3.to_checksum_address(pair_address),
        abi=self._pair_abi,
      )
      reserves = pool.functions.getReserves().call()
      token0 = pool.functions.token0().call()
      r0, r1 = int(reserves[0]), int(reserves[1])
      base_is_token0 = token0.lower() == token_a.lower()
      base_reserve = r0 if base_is_token0 else r1
      quote_reserve = r1 if base_is_token0 else r0
      return base_reserve >= _min_reserve(pair.base) and quote_reserve >= _min_reserve(pair.quote)
    except Exception: # noqa: BLE001
      return False

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
