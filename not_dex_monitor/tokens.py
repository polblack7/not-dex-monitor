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
  "CBBTC": Token("CBBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8),
  "WSTETH": Token("WSTETH", "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", 18),
  "USDS": Token("USDS", "0xdC035D45d973E3EC169d2276DDab16f1e407384F", 18),
  "USDE": Token("USDE", "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3", 18),
  "GHO": Token("GHO", "0x40D16FC0246aD3160Ccc09B8D0D3A2cD28aE6C2f", 18),
  "FRAX": Token("FRAX", "0x853d955aCEf822Db058eb8505911ED77F175b99e", 18),
  "PEPE": Token("PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18),
}

_ALIASES: Dict[str, str] = {
  "ETH": "WETH",
}

_DEFAULT_BASE_AMOUNTS: Dict[str, Decimal] = {
  "WETH": Decimal("1"),
  "WBTC": Decimal("0.1"),
  "CBBTC": Decimal("0.1"),
  "WSTETH": Decimal("1"),
  "PEPE": Decimal("10000000"), # ~$100 worth at typical price
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
