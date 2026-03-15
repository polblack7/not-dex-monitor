from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Dict, List, Optional, Set

from web3 import Web3

from .backend_client import AuthError, BackendClient, BackendError
from .config import Config
from .dex import canonical_dex_name, create_quoters, normalize_dex_name
from .executor import execute_arb
from .models import Opportunity, PriceQuote, Settings, compute_expected_profit_pct
from .quote_math import price_from_amounts, to_wei
from .tokens import Token, default_base_amount, get_token, parse_pair
from .util.logging import get_logger
from .util.time import utcnow_iso


logger = get_logger(__name__)


class WalletWorker:
    def __init__(
        self,
        wallet_address: str,
        backend: BackendClient,
        config: Config,
        *,
        settings_override: Optional[Settings] = None,
        dry_run: bool = False,
    ) -> None:
        self.wallet_address = wallet_address
        self.backend = backend
        self.config = config
        self._settings_override = settings_override
        self._dry_run = dry_run
        self._stop_event = asyncio.Event()
        self._jwt: Optional[str] = None
        self._jwt_expires_at = 0.0
        self._last_settings_fetch = 0.0
        self._settings: Optional[Settings] = None
        self._last_heartbeat = 0.0
        self._log_once_keys: Set[str] = set()

        self.w3 = Web3(
            Web3.HTTPProvider(
                self.config.eth_rpc_url,
                request_kwargs={"timeout": self.config.http_timeout_sec},
            )
        )
        self.dex_quoters = create_quoters(self.w3)

    async def run(self) -> None:
        await self._send_status("active", None)
        while not self._stop_event.is_set():
            start_time = time.monotonic()
            settings: Optional[Settings] = None
            try:
                settings = await self._get_settings()
                opportunities = await self._scan(settings)
                scan_time_ms = int((time.monotonic() - start_time) * 1000)
                for opp in opportunities:
                    await self._emit_opportunity(opp, scan_time_ms)

                await self._maybe_heartbeat(settings)
            except AuthError:
                self._jwt = None
                await self._log("warning", "JWT invalid, refreshing", {"wallet": self.wallet_address})
            except Exception as exc:  # noqa: BLE001
                await self._send_status("error", str(exc))
                await self._log(
                    "error",
                    "Worker error",
                    {"wallet": self.wallet_address, "error": str(exc)},
                )

            scan_frequency = settings.scan_frequency_sec if settings else 10
            await self._sleep_or_stop(scan_frequency - (time.monotonic() - start_time))

        await self._send_status("stopped", None)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _get_settings(self) -> Settings:
        if self._settings_override is not None:
            return self._settings_override
        now = time.monotonic()
        if self._settings and now - self._last_settings_fetch < self._settings_refresh_sec():
            return self._settings

        await self._ensure_jwt()
        try:
            data = await self.backend.get_settings(self._jwt or "")
        except AuthError:
            self._jwt = None
            await self._ensure_jwt()
            data = await self.backend.get_settings(self._jwt or "")

        settings = Settings.model_validate(data)
        self._settings = settings
        self._last_settings_fetch = now
        return settings

    def _settings_refresh_sec(self) -> int:
        if not self._settings:
            return 30
        return max(60, self._settings.scan_frequency_sec)

    async def _ensure_jwt(self) -> None:
        if self._dry_run:
            return
        if not self.config.access_token_master:
            raise BackendError("ACCESS_TOKEN_MASTER is required to login")
        if self._jwt and time.monotonic() < self._jwt_expires_at:
            return
        token = await self.backend.login(self.wallet_address, self.config.access_token_master)
        self._jwt = token
        self._jwt_expires_at = time.monotonic() + self.config.jwt_cache_ttl_sec

    async def _scan(self, settings: Settings) -> List[Opportunity]:
        if not settings.dex_list or not settings.pairs:
            return []

        try:
            gas_price_wei = await asyncio.to_thread(lambda: int(self.w3.eth.gas_price))
        except Exception as exc:  # noqa: BLE001
            await self._log_once(
                "gas_price_error",
                "warning",
                "Failed to fetch gas price; using fallback",
                {"error": str(exc)},
            )
            gas_price_wei = int(Decimal("20") * Decimal(10**9))
        gas_price_gwei = float(Decimal(gas_price_wei) / Decimal(10**9))

        opportunities: List[Opportunity] = []

        for pair_str in settings.pairs:
            if self._stop_event.is_set():
                break
            try:
                pair = parse_pair(pair_str)
            except ValueError as exc:
                await self._log_once("pair_invalid", "warning", str(exc), {"pair": pair_str})
                continue
            print(f"Checking pair {pair.raw}")

            base_amount = default_base_amount(pair.base)
            amount_in = to_wei(base_amount, pair.base.decimals)

            quotes: List[PriceQuote] = []
            for dex_name in settings.dex_list:
                if self._stop_event.is_set():
                    break
                canonical = canonical_dex_name(dex_name)
                key = normalize_dex_name(canonical)
                quoter = self.dex_quoters.get(key)
                if not quoter:
                    await self._log_once(
                        f"dex_missing_{dex_name}",
                        "warning",
                        f"Unsupported DEX: {dex_name}",
                        {"dex": dex_name},
                    )
                    print(f"Checked dex {dex_name} for {pair.raw}: unsupported dex")
                    continue

                try:
                    supports_pair = quoter.supports_pair(pair)
                except Exception as exc:  # noqa: BLE001
                    await self._log_once(
                        f"dex_supports_error_{key}_{pair.raw}",
                        "warning",
                        f"{quoter.name} supports_pair failed",
                        {"dex": quoter.name, "pair": pair.raw, "error": str(exc)},
                    )
                    print(f"Checked dex {quoter.name} for {pair.raw}: supports_pair error {exc}")
                    continue

                if not supports_pair:
                    await self._log_once(
                        f"dex_pair_unsupported_{key}_{pair.raw}",
                        "info",
                        f"{quoter.name} does not support {pair.raw}",
                        {"dex": quoter.name, "pair": pair.raw},
                    )
                    print(f"Checked dex {quoter.name} for {pair.raw}: unsupported pair")
                    continue

                result = await quoter.quote_exact_in_async(pair.base, pair.quote, amount_in)
                if not result.ok:
                    await self._log_once(
                        f"dex_quote_error_{key}_{pair.raw}",
                        "warning",
                        f"{quoter.name} quote failed",
                        {
                            "dex": quoter.name,
                            "pair": pair.raw,
                            "error": result.error,
                            "diagnostics": result.diagnostics,
                        },
                    )
                    print(f"Checked dex {quoter.name} for {pair.raw}: quote error {result.error}")
                    continue

                price = price_from_amounts(
                    amount_in,
                    result.amount_out_wei or 0,
                    pair.base.decimals,
                    pair.quote.decimals,
                )
                print(
                    f"Checked dex {quoter.name} for {pair.raw}: amount_out {result.amount_out_wei} "
                    f"price {price:.8f}"
                )
                quotes.append(
                    PriceQuote(
                        dex=quoter.name,
                        amount_in=amount_in,
                        amount_out=result.amount_out_wei or 0,
                        price=price,
                    )
                )

            if len(quotes) < 2:
                print(f"Checked pair {pair.raw}: insufficient quotes ({len(quotes)})")
                continue

            quotes_sorted = sorted(quotes, key=lambda q: q.price)
            buy_quote = quotes_sorted[0]
            sell_quote = quotes_sorted[-1]

            if buy_quote.price <= 0 or sell_quote.price <= buy_quote.price:
                continue

            gas_used = self._gas_used_estimate(buy_quote.dex, sell_quote.dex)
            fees_in_quote = await self._estimate_fees_in_quote(
                pair.quote,
                gas_price_wei,
                gas_used,
                settings.dex_list,
            )
            base_amount_hr = Decimal(amount_in) / Decimal(10**pair.base.decimals)
            expected_profit_pct = compute_expected_profit_pct(
                buy_price=buy_quote.price,
                sell_price=sell_quote.price,
                base_amount=base_amount_hr,
                fees_in_quote=fees_in_quote,
            )
            print(
                f"Checked pair {pair.raw}: buy {buy_quote.dex} {buy_quote.price:.8f} "
                f"sell {sell_quote.dex} {sell_quote.price:.8f} expected_profit_pct {expected_profit_pct:.6f}"
            )

            if float(expected_profit_pct) < settings.min_profit_pct:
                print(
                    f"Checked pair {pair.raw}: expected_profit_pct {expected_profit_pct:.6f} "
                    f"below min {settings.min_profit_pct:.6f}"
                )
                continue

            spread_pct = (sell_quote.price - buy_quote.price) / buy_quote.price * Decimal("100")
            liquidity_score = float(max(Decimal("0.1"), min(Decimal("0.99"), Decimal("1") - spread_pct / Decimal("10"))))

            opp = Opportunity(
                pair=pair.raw,
                buy_dex=buy_quote.dex,
                sell_dex=sell_quote.dex,
                expected_profit_pct=float(expected_profit_pct),
                liquidity_score=liquidity_score,
                gas_price_gwei=gas_price_gwei,
                route=[pair.base.symbol, pair.quote.symbol],
                fees_quote=float(fees_in_quote),
            )
            opportunities.append(opp)

        return opportunities

    async def scan_once(self, settings: Settings) -> List[Opportunity]:
        return await self._scan(settings)

    def _gas_used_estimate(self, buy_dex: str, sell_dex: str) -> int:
        total = 0
        for name in {buy_dex, sell_dex}:
            key = normalize_dex_name(canonical_dex_name(name))
            quoter = self.dex_quoters.get(key)
            if quoter:
                total += int(getattr(quoter, "gas_estimate", 180_000))
        if total == 0:
            total = 180_000
        return total

    async def _estimate_fees_in_quote(
        self,
        quote_token: Token,
        gas_price_wei: int,
        gas_used: int,
        dex_list: List[str],
    ) -> Decimal:
        gas_cost_eth = Decimal(gas_price_wei) * Decimal(gas_used) / Decimal(10**18)
        if quote_token.symbol == "WETH":
            return gas_cost_eth

        eth_token = get_token("WETH")
        amount_in = to_wei(Decimal("1"), eth_token.decimals)
        for dex_name in dex_list:
            key = normalize_dex_name(canonical_dex_name(dex_name))
            quoter = self.dex_quoters.get(key)
            if not quoter:
                continue
            result = await quoter.quote_exact_in_async(eth_token, quote_token, amount_in)
            if result.ok and result.amount_out_wei:
                price = price_from_amounts(
                    amount_in,
                    result.amount_out_wei,
                    eth_token.decimals,
                    quote_token.decimals,
                )
                return gas_cost_eth * price

        await self._log_once(
            f"fees_missing_{quote_token.symbol}",
            "warning",
            "Unable to estimate gas fees in quote token",
            {"quote": quote_token.symbol},
        )
        return Decimal("0")

    async def _emit_opportunity(
        self,
        opp: Opportunity,
        exec_time_ms: int,
    ) -> None:
        message = (
            f"Arb {opp.pair}: buy on {opp.buy_dex}, sell on {opp.sell_dex}, "
            f"expected {opp.expected_profit_pct:.2f}%"
        )
        print(message)
        await self.backend.post_event(
            self.wallet_address,
            "opportunity",
            {
                "message": message,
                "pair": opp.pair,
                "buy_dex": opp.buy_dex,
                "sell_dex": opp.sell_dex,
                "expected_profit_pct": opp.expected_profit_pct,
                "liquidity_score": opp.liquidity_score,
                "gas_price_gwei": opp.gas_price_gwei,
                "route": opp.route,
                "timestamp": utcnow_iso(),
            },
        )

        # Try auto-execution if user has stored a wallet key
        executed = await self._try_execute(opp, exec_time_ms)
        if executed:
            return

        if self.config.emit_ops_on_opportunity:
            await self.backend.post_event(
                self.wallet_address,
                "op",
                {
                    "timestamp": utcnow_iso(),
                    "pair": opp.pair,
                    "dex": f"{opp.buy_dex}->{opp.sell_dex}",
                    "profit": 0.0,
                    "fees": opp.fees_quote,
                    "exec_time_ms": exec_time_ms,
                    "status": "fail",
                    "error_message": "EXECUTION_NOT_IMPLEMENTED",
                },
            )

    async def _try_execute(self, opp: Opportunity, scan_time_ms: int) -> bool:
        """Attempt to auto-execute the arb. Returns True if execution was attempted."""
        if not self.config.wallet_encryption_key:
            return False

        try:
            encrypted_key = await self.backend.get_wallet_key(self.wallet_address)
        except Exception as exc:  # noqa: BLE001
            await self._log("warning", "Failed to fetch wallet key", {"error": str(exc)})
            return False

        if not encrypted_key:
            return False

        settings = self._settings or Settings()
        await self._log("info", f"Auto-executing arb: {opp.pair} {opp.buy_dex}->{opp.sell_dex}", {})

        result = await execute_arb(
            self.w3,
            encrypted_key,
            self.config.wallet_encryption_key,
            self.wallet_address,
            opp,
            settings,
        )

        await self.backend.post_event(
            self.wallet_address,
            "op",
            {
                "timestamp": utcnow_iso(),
                "pair": opp.pair,
                "dex": f"{opp.buy_dex}->{opp.sell_dex}",
                "profit": result.profit,
                "fees": result.fees,
                "exec_time_ms": result.exec_time_ms,
                "status": "success" if result.success else "fail",
                "error_message": result.error,
            },
        )

        if result.success:
            await self._log("info", f"Arb executed: profit {result.profit}", {
                "tx_hash": result.tx_hash, "pair": opp.pair,
            })
        else:
            await self._log("warning", f"Arb execution failed: {result.error}", {
                "pair": opp.pair, "tx_hash": result.tx_hash,
            })

        return True

    async def _send_status(self, status: str, last_error: Optional[str]) -> None:
        if self._dry_run:
            logger.info("Dry-run status: %s", {"status": status, "last_error": last_error})
            return
        await self.backend.post_event(
            self.wallet_address,
            "status",
            {"status": status, "last_error": last_error},
        )

    async def _log(self, level: str, message: str, context: Dict[str, object]) -> None:
        if self._dry_run:
            logger.info("Dry-run log: %s", {"level": level, "message": message, "context": context})
            return
        await self.backend.post_event(
            self.wallet_address,
            "log",
            {"level": level, "message": message, "context": context},
        )

    async def _log_once(self, key: str, level: str, message: str, context: Dict[str, object]) -> None:
        if key in self._log_once_keys:
            return
        self._log_once_keys.add(key)
        await self._log(level, message, context)

    async def _maybe_heartbeat(self, settings: Settings) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < max(60, settings.scan_frequency_sec * 2):
            return
        self._last_heartbeat = now
        await self._log(
            "info",
            "Worker heartbeat",
            {"wallet": self.wallet_address, "scan_frequency_sec": settings.scan_frequency_sec},
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
