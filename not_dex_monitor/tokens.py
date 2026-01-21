from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class TokenPair:
    base: Token
    quote: Token
    raw: str


_TOKEN_REGISTRY: Dict[str, Token] = {
    "WETH": Token("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
    "USDT": Token("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    "USDC": Token("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    "DAI": Token("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
    "WBTC": Token("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
}

_ALIASES: Dict[str, str] = {
    "ETH": "WETH",
}

_DEFAULT_BASE_AMOUNTS: Dict[str, Decimal] = {
    "WETH": Decimal("1"),
    "WBTC": Decimal("0.1"),
}


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def get_token(symbol: str) -> Token:
    normalized = normalize_symbol(symbol)
    resolved = _ALIASES.get(normalized, normalized)
    if resolved not in _TOKEN_REGISTRY:
        raise ValueError(f"Unknown token symbol: {symbol}")
    return _TOKEN_REGISTRY[resolved]


def parse_pair(pair: str) -> TokenPair:
    if "/" not in pair:
        raise ValueError(f"Invalid pair format: {pair}")
    base_sym, quote_sym = pair.split("/", 1)
    base = get_token(base_sym)
    quote = get_token(quote_sym)
    return TokenPair(base=base, quote=quote, raw=pair)


def default_base_amount(token: Token) -> Decimal:
    return _DEFAULT_BASE_AMOUNTS.get(token.symbol, Decimal("1"))
