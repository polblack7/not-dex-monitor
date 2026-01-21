from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .backend_client import BackendClient
from .config import Config
from .dry_run import run_dry_run
from .supervisor import Supervisor
from .util.logging import configure_logging, get_logger


logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DEX arbitrage opportunity monitor")
    parser.add_argument("--backend-url", dest="backend_url", help="Backend base URL")
    parser.add_argument("--poll-sec", dest="poll_sec", type=int, help="Active users poll interval")
    parser.add_argument("--log-level", dest="log_level", default="INFO", help="Logging level")
    parser.add_argument("--dry-run", action="store_true", help="Run without backend and print opportunities")
    parser.add_argument("--wallet", dest="wallet", help="Wallet address for dry-run mode")
    parser.add_argument("--dry-run-iterations", dest="dry_run_iterations", type=int, default=3)
    parser.add_argument(
        "--dry-run-settings",
        dest="dry_run_settings",
        default=str(Path(__file__).resolve().parent / "examples" / "settings.json"),
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    configure_logging(args.log_level)
    config = Config.from_env(
        backend_base_url=args.backend_url,
        active_users_poll_sec=args.poll_sec,
        require_backend=not args.dry_run,
    )

    if args.dry_run:
        if not args.wallet:
            raise ValueError("--wallet is required for --dry-run")
        settings_path = Path(args.dry_run_settings)
        await run_dry_run(
            wallet_address=args.wallet,
            settings_path=settings_path,
            iterations=max(1, int(args.dry_run_iterations)),
            config=config,
        )
        return

    if config.access_token_master:
        logger.warning("ACCESS_TOKEN_MASTER is set; use only in dev/MVP environments")
    if config.chain_id != 1:
        logger.warning("CHAIN_ID is %s; token/DEX registry is mainnet-specific", config.chain_id)

    async with BackendClient(
        base_url=config.backend_base_url,
        internal_api_key=config.internal_api_key,
        timeout_sec=config.http_timeout_sec,
        max_retries=config.http_max_retries,
        backoff_base_sec=config.http_backoff_base_sec,
    ) as backend:
        supervisor = Supervisor(backend=backend, config=config)
        await supervisor.run()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
