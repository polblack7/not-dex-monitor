from __future__ import annotations

import asyncio
import json
from pathlib import Path
from .config import Config
from .models import Opportunity, Settings
from .util.logging import get_logger
from .worker import WalletWorker


logger = get_logger(__name__)


class _NullBackend:
    async def post_event(self, wallet_address: str, event_type: str, payload: dict) -> None:  # noqa: ANN001
        logger.info("Dry-run event: %s", {"wallet": wallet_address, "type": event_type, "payload": payload})


def load_settings(path: Path) -> Settings:
    data = json.loads(path.read_text())
    return Settings.model_validate(data)


async def run_dry_run(
    *,
    wallet_address: str,
    settings_path: Path,
    iterations: int,
    config: Config,
) -> None:
    settings = load_settings(settings_path)
    worker = WalletWorker(
        wallet_address=wallet_address,
        backend=_NullBackend(),
        config=config,
        settings_override=settings,
        dry_run=True,
    )

    for index in range(iterations):
        logger.info("Dry-run scan %s/%s", index + 1, iterations)
        opportunities = await worker.scan_once(settings)
        _print_opportunities(opportunities)
        if index + 1 < iterations:
            await asyncio.sleep(max(1, settings.scan_frequency_sec))


def _print_opportunities(opps: list[Opportunity]) -> None:
    if not opps:
        logger.info("Dry-run: no opportunities found")
        return
    for opp in opps:
        logger.info(
            "Dry-run opportunity: %s",
            {
                "pair": opp.pair,
                "buy": opp.buy_dex,
                "sell": opp.sell_dex,
                "expected_profit_pct": opp.expected_profit_pct,
            },
        )
