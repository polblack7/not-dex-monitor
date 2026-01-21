from __future__ import annotations

import argparse
import os
from decimal import Decimal
from typing import Dict

from web3 import Web3

from .dex import create_quoters
from .quote_math import to_wei
from .tokens import parse_pair
from .util.logging import configure_logging, get_logger


logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DEX fork sweep")
    parser.add_argument("--rpc-url", dest="rpc_url", default="http://127.0.0.1:8545")
    parser.add_argument(
        "--pairs",
        dest="pairs",
        default="WETH/USDC,WETH/USDT,WBTC/WETH",
        help="Comma-separated pairs to sweep",
    )
    parser.add_argument("--min-success", dest="min_success", type=int, default=_min_success_default())
    parser.add_argument("--log-level", dest="log_level", default="INFO")
    return parser.parse_args()


def _min_success_default() -> int:
    raw = os.getenv("DEX_FORK_MIN_SUCCESS")
    if not raw:
        return 6
    try:
        return max(1, int(raw))
    except ValueError:
        return 6


def sweep_quotes(w3: Web3, pairs: list[str]) -> Dict[str, Dict[str, bool]]:
    adapters = create_quoters(w3)
    results: Dict[str, Dict[str, bool]] = {}
    for pair_str in pairs:
        pair = parse_pair(pair_str)
        amount_in = to_wei(Decimal("1"), pair.base.decimals)
        per_pair: Dict[str, bool] = {}
        for key, adapter in adapters.items():
            if not adapter.supports_pair(pair):
                per_pair[adapter.name] = False
                continue
            result = adapter.quote_exact_in(pair.base, pair.quote, amount_in)
            per_pair[adapter.name] = bool(result.ok)
        results[pair_str] = per_pair
    return results


def assert_min_success(results: Dict[str, Dict[str, bool]], pair: str, min_success: int) -> None:
    per_pair = results.get(pair, {})
    ok_count = sum(1 for ok in per_pair.values() if ok)
    if ok_count < min_success:
        raise AssertionError(
            f"Only {ok_count} venues returned quotes for {pair}; expected >= {min_success}."
        )


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    w3 = Web3(Web3.HTTPProvider(args.rpc_url, request_kwargs={"timeout": 15}))

    results = sweep_quotes(w3, pairs)
    for pair, per_pair in results.items():
        logger.info("Sweep results", extra={"pair": pair, "results": per_pair})

    target_pair = pairs[0] if pairs else "WETH/USDC"
    assert_min_success(results, target_pair, args.min_success)
    logger.info("Fork sweep passed", extra={"pair": target_pair, "min_success": args.min_success})


if __name__ == "__main__":
    main()
