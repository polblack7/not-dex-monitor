"""Fluid DEX adapter (Instadapp Fluid Protocol).

Fluid has no Quoter contract -- instead each pool exposes a "revert as quote"
pattern. Calling `swapIn(zeroToOne, amountIn, 0, ADDRESS_DEAD)` is *guaranteed*
to revert with custom error `FluidDexSwapResult(uint256 amountOut)`. The
adapter performs an `eth_call`, catches the revert, and decodes the result.

Each Fluid pool is its own contract -- there is no shared factory. Pool
addresses for the 12-token universe are hard-coded below; the on-chain
"token0 < token1" convention determines swap direction.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from .base import BaseDexAdapter, QuoteResult
from ..tokens import Token, TokenPair


ADDRESS_DEAD = "0x000000000000000000000000000000000000dEaD"

# 4-byte selectors computed at import time.
SWAP_IN_SELECTOR = keccak(text="swapIn(bool,uint256,uint256,address)")[:4]
SWAP_RESULT_SELECTOR = keccak(text="FluidDexSwapResult(uint256)")[:4]


# Pool registry keyed by sorted (symbol_a, symbol_b). Verified live via
# GeckoTerminal API -- only pools with non-trivial TVL on Ethereum mainnet.
# Adding new pools: drop the address in here, no other changes needed.
FLUID_POOLS: Dict[Tuple[str, str], str] = {
  # USDC/USDT -- $69M TVL, $91M 24h volume (largest Fluid pool overall)
  ("USDC", "USDT"): "0x667701e51b4d1ca244f17c78f7ab8744b4c99f9b",
  # USDe/USDT -- $40M TVL
  ("USDE", "USDT"): "0xf063bd202e45d6b2843102cb4ece339026645d4a",
  # cbBTC/WBTC -- $24M TVL
  ("CBBTC", "WBTC"): "0x3c0441b42195f4ad6aa9a0978e06096ea616cda7",
  # GHO/USDC -- $21.5M TVL
  ("GHO", "USDC"): "0xde632c3a214d5f14c1d8ddf0b92f8bcd188fee45",
}


class FluidDexAdapter(BaseDexAdapter):
  """Fluid DEX adapter using the revert-as-quote pattern."""

  def __init__(self, w3: Web3) -> None:
    super().__init__(w3, name="Fluid DEX", gas_estimate=200_000)

  def _supports_pair(self, pair: TokenPair) -> bool:
    return self._find_pool(pair.base.symbol, pair.quote.symbol) is not None

  def _quote_exact_in(self, token_in: Token, token_out: Token, amount_in_wei: int) -> QuoteResult:
    pool_address = self._find_pool(token_in.symbol, token_out.symbol)
    if pool_address is None:
      return self._error_result(token_in, token_out, amount_in_wei, error="unsupported_pair")

    token_in_addr = self.w3.to_checksum_address(token_in.address)
    token_out_addr = self.w3.to_checksum_address(token_out.address)
    # Fluid follows the standard EVM convention: token0 has the smaller
    # address. zeroToOne = (token_in is token0).
    zero_to_one = token_in_addr.lower() < token_out_addr.lower()

    calldata = SWAP_IN_SELECTOR + abi_encode(
      ["bool", "uint256", "uint256", "address"],
      [zero_to_one, int(amount_in_wei), 0, ADDRESS_DEAD],
    )

    # The call is *expected* to revert with FluidDexSwapResult(uint256).
    # Use the raw provider so we can parse the revert payload regardless
    # of web3.py version-specific exception wrapping.
    response = self.w3.provider.make_request(
      "eth_call",
      [
        {
          "to": self.w3.to_checksum_address(pool_address),
          "data": "0x" + calldata.hex(),
        },
        "latest",
      ],
    )

    revert_data = self._extract_revert_data(response)
    if revert_data is None:
      return self._error_result(
        token_in, token_out, amount_in_wei,
        error="no_revert_payload",
        diagnostics={"pool": pool_address, "response": str(response)[:200]},
      )

    if not revert_data.startswith(SWAP_RESULT_SELECTOR):
      # Some other revert (e.g. liquidity / amount limit). Treat as a
      # legitimate "quote failed" -- the worker will skip silently.
      return self._error_result(
        token_in, token_out, amount_in_wei,
        error="quote_revert",
        diagnostics={
          "pool": pool_address,
          "zero_to_one": zero_to_one,
          "revert_selector": revert_data[:4].hex(),
          "revert_data": revert_data[:64].hex(),
        },
      )

    amount_out = int.from_bytes(revert_data[4:36], "big")
    if amount_out <= 0:
      return self._error_result(token_in, token_out, amount_in_wei, error="zero_quote")

    return QuoteResult(
      amount_out_wei=amount_out,
      venue=self.name,
      route=[token_in.symbol, token_out.symbol],
      gas_estimate=self.gas_estimate,
      error=None,
      diagnostics={"pool": pool_address, "zero_to_one": zero_to_one},
    )

  @staticmethod
  def _find_pool(symbol_in: str, symbol_out: str) -> Optional[str]:
    key = tuple(sorted([symbol_in.upper(), symbol_out.upper()]))
    return FLUID_POOLS.get(key)

  @staticmethod
  def _extract_revert_data(response: dict) -> Optional[bytes]:
    """Pull raw revert bytes out of a JSON-RPC eth_call response.

    Geth/Erigon, Alchemy, Infura, and Anvil all wrap revert data slightly
    differently. We look in every documented field before giving up.
    """
    err = response.get("error") if isinstance(response, dict) else None
    if not err:
      return None
    for key in ("data", "originalError"):
      value = err.get(key) if isinstance(err, dict) else None
      if isinstance(value, dict):
        inner = value.get("data")
        if isinstance(inner, str):
          return _hex_to_bytes(inner)
      elif isinstance(value, str):
        hex_data = _hex_to_bytes(value)
        if hex_data is not None:
          return hex_data
    # Some providers nest the hex inside the message itself.
    msg = err.get("message", "") if isinstance(err, dict) else ""
    if "0x" in msg:
      chunk = msg[msg.index("0x"):].split()[0].rstrip(".,)")
      return _hex_to_bytes(chunk)
    return None


def _hex_to_bytes(value: str) -> Optional[bytes]:
  if not isinstance(value, str):
    return None
  cleaned = value.strip()
  if cleaned.startswith("0x"):
    cleaned = cleaned[2:]
  if not cleaned or any(c not in "0123456789abcdefABCDEF" for c in cleaned):
    return None
  if len(cleaned) % 2 == 1:
    cleaned = "0" + cleaned
  try:
    return bytes.fromhex(cleaned)
  except ValueError:
    return None
