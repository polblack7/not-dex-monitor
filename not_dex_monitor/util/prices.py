"""USD price oracle for base tokens used in profit accounting.

Uses Uniswap V3 QuoterV2 against the deepest USDC pool for each token.
Results are cached for `CACHE_TTL_SEC` to avoid hammering RPC -- token spot
prices barely move within a single arb-execution timeframe.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from web3 import Web3

from ..dex.abi import load_abi
from ..dex.addresses import MAINNET_ADDRESSES


# (token_in, fee_tier) routing table. Picked fee tier per token based on which
# pool is canonical / deepest on mainnet for token->USDC.
_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_USD_ROUTES: Dict[str, Tuple[str, int]] = {
    "USDC":   (_USDC, 0),                    # 1:1 trivially
    "USDT":   ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 100),
    "DAI":    ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 100),
    "WETH":   ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 500),
    "WBTC":   ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 3000),
    "CBBTC":  ("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 500),
    "WSTETH": ("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", 100),
    "USDS":   ("0xdC035D45d973E3EC169d2276DDab16f1e407384F", 100),
    "USDE":   ("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3", 100),
    "GHO":    ("0x40D16FC0246aD3160Ccc09B8D0D3A2cD28aE6C2f", 500),
    "FRAX":   ("0x853d955aCEf822Db058eb8505911ED77F175b99e", 500),
}

_DECIMALS: Dict[str, int] = {
    "USDC": 6, "USDT": 6, "DAI": 18, "WETH": 18, "WBTC": 8, "CBBTC": 8,
    "WSTETH": 18, "USDS": 18, "USDE": 18, "GHO": 18, "FRAX": 18,
}

_CACHE: Dict[str, Tuple[float, float]] = {}  # symbol -> (price_usd, expires_at)
CACHE_TTL_SEC = 60


def get_usd_price(w3: Web3, token_symbol: str) -> Optional[float]:
    """Return mid-price of `token_symbol` in USD, or None if unknown.

    Uses a one-token UniV3 quote (`1 token -> USDC`) against the most liquid
    fee tier. Returns None silently if the token isn't routed; caller should
    fall back to keeping the native value if no rate is available.
    """
    symbol = token_symbol.upper()
    if symbol == "USDC":
        return 1.0
    route = _USD_ROUTES.get(symbol)
    if not route:
        return None

    cached = _CACHE.get(symbol)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]

    token_addr, fee = route
    decimals = _DECIMALS.get(symbol, 18)
    one_token_wei = 10 ** decimals

    try:
        quoter = w3.eth.contract(
            address=w3.to_checksum_address(MAINNET_ADDRESSES.uniswap_v3.quoter_v2),
            abi=load_abi("uniswap_v3", "quoter_v2"),
        )
        result = quoter.functions.quoteExactInputSingle(
            (w3.to_checksum_address(token_addr),
             w3.to_checksum_address(_USDC),
             one_token_wei, fee, 0)
        ).call({"gas": 1_000_000})
        amount_out = int(result[0]) if isinstance(result, (list, tuple)) else int(result)
        price = amount_out / 10 ** 6  # USDC has 6 decimals
        _CACHE[symbol] = (price, now + CACHE_TTL_SEC)
        return price
    except Exception:  # noqa: BLE001
        return None


def to_usd(amount_native: float, token_symbol: str, w3: Web3) -> Optional[float]:
    """Convert an amount expressed in `token_symbol` units to USD."""
    if amount_native == 0:
        return 0.0
    price = get_usd_price(w3, token_symbol)
    if price is None:
        return None
    return float(amount_native) * price
