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
from .models import Opportunity, PriceQuote, Settings
from .quote_math import price_from_amounts, to_wei
from .tokens import Token, default_base_amount, get_token, parse_pair
from .util.logging import get_logger
from .util.time import utcnow_iso


logger = get_logger(__name__)

_SEP = "═" * 58
_DIV = "─" * 58


def _fmt_amount(amount_wei: int, decimals: int, symbol: str) -> str:
    value = Decimal(amount_wei) / Decimal(10 ** decimals)
    if value >= Decimal("10000"):
        formatted = f"{value:,.2f}"
    elif value >= Decimal("1"):
        formatted = f"{value:.4f}"
    elif value >= Decimal("0.001"):
        formatted = f"{value:.6f}"
    else:
        formatted = f"{value:.8f}"
    return f"{formatted} {symbol}"


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

            base_amount = default_base_amount(pair.base)
            amount_in = to_wei(base_amount, pair.base.decimals)

            print(f"\n{_SEP}")
            print(
                f"  Pair {pair.raw}"
                f"  |  in: {_fmt_amount(amount_in, pair.base.decimals, pair.base.symbol)}"
                f"  |  gas: {gas_price_gwei:.1f} gwei"
            )
            print(_SEP)

            # Phase 1: forward quotes — base → quote on each DEX.
            forward_quotes = await self._collect_forward_quotes(pair, amount_in, settings.dex_list)
            if not forward_quotes:
                print(f"  No forward quotes — skipping pair.")
                continue

            print(f"\n  Forward  {pair.base.symbol} → {pair.quote.symbol}:")
            for fwd in forward_quotes:
                print(f"    {fwd.dex:<20}  {_fmt_amount(fwd.amount_out, pair.quote.decimals, pair.quote.symbol)}")

            # Phase 2: for each forward result, get reverse quotes — quote → base
            # on ALL DEXes (including the same one). This models the actual round
            # trip: spend base, receive quote on buy_dex, then spend that quote
            # and receive base on sell_dex.
            pair_opps: List[Opportunity] = []

            for fwd in forward_quotes:
                if fwd.amount_out == 0:
                    continue

                reverse_quotes = await self._collect_reverse_quotes(pair, fwd.amount_out, settings.dex_list)
                if not reverse_quotes:
                    continue

                print(f"\n  Buy {fwd.dex}  →  {_fmt_amount(fwd.amount_out, pair.quote.decimals, pair.quote.symbol)}:")

                for rev in reverse_quotes:
                    final_base_wei = rev.amount_out

                    gas_used = self._gas_used_estimate(fwd.dex, rev.dex)
                    fees_in_base = await self._estimate_fees_in_base(
                        pair.base, gas_price_wei, gas_used, settings.dex_list,
                    )
                    fees_in_base_wei = to_wei(fees_in_base, pair.base.decimals)

                    profit_wei = final_base_wei - amount_in - fees_in_base_wei
                    profit_pct = float(Decimal(profit_wei) / Decimal(amount_in) * Decimal("100"))
                    profit_str = f"{profit_pct:+.4f}%"

                    is_opportunity = profit_pct >= settings.min_profit_pct
                    tag = "★ " if is_opportunity else "  "
                    print(
                        f"    {tag}sell {rev.dex:<18}  →  "
                        f"{_fmt_amount(final_base_wei, pair.base.decimals, pair.base.symbol):<24}  "
                        f"{profit_str}"
                    )

                    if not is_opportunity:
                        continue

                    gross_spread_pct = float(
                        abs(Decimal(final_base_wei) - Decimal(amount_in))
                        / Decimal(amount_in) * Decimal("100")
                    )
                    # Smaller gross deviation from 1:1 → more liquid → higher score.
                    # 0% spread → 0.99; ≥ 9% → 0.10.
                    liquidity_score = float(max(
                        Decimal("0.10"),
                        min(Decimal("0.99"), Decimal("1") - Decimal(str(gross_spread_pct)) / Decimal("9")),
                    ))

                    opp = Opportunity(
                        pair=pair.raw,
                        buy_dex=fwd.dex,
                        sell_dex=rev.dex,
                        expected_profit_pct=profit_pct,
                        liquidity_score=liquidity_score,
                        gas_price_gwei=gas_price_gwei,
                        route=[pair.base.symbol, pair.quote.symbol, pair.base.symbol],
                        fees_base=float(fees_in_base),
                    )
                    pair_opps.append(opp)
                    opportunities.append(opp)

            print(f"\n  {_DIV}")
            if pair_opps:
                n = len(pair_opps)
                print(f"  ★ {n} opportunit{'y' if n == 1 else 'ies'} found for {pair.raw}:")
                for o in pair_opps:
                    print(f"    {o.buy_dex} → {o.sell_dex}  {o.expected_profit_pct:+.4f}%")
            else:
                print(f"  No opportunities for {pair.raw}")
            print(f"  {_DIV}")

        print(f"\n{_SEP}")
        n_total = len(opportunities)
        print(f"  Scan complete  |  {n_total} opportunit{'y' if n_total == 1 else 'ies'} found")
        print(f"{_SEP}\n")

        return opportunities

    async def _collect_forward_quotes(
        self, pair, amount_in: int, dex_list: List[str]
    ) -> List[PriceQuote]:
        """Quote base→quote on every DEX, using supports_pair to skip unsupported ones."""
        quotes: List[PriceQuote] = []
        for dex_name in dex_list:
            if self._stop_event.is_set():
                break
            canonical = canonical_dex_name(dex_name)
            key = normalize_dex_name(canonical)
            quoter = self.dex_quoters.get(key)
            if not quoter:
                await self._log_once(
                    f"dex_missing_{dex_name}", "warning",
                    f"Unsupported DEX: {dex_name}", {"dex": dex_name},
                )
                continue

            try:
                supports = quoter.supports_pair(pair)
            except Exception as exc:  # noqa: BLE001
                await self._log_once(
                    f"dex_supports_error_{key}_{pair.raw}", "warning",
                    f"{quoter.name} supports_pair failed",
                    {"dex": quoter.name, "pair": pair.raw, "error": str(exc)},
                )
                continue

            if not supports:
                await self._log_once(
                    f"dex_pair_unsupported_{key}_{pair.raw}", "info",
                    f"{quoter.name} does not support {pair.raw}",
                    {"dex": quoter.name, "pair": pair.raw},
                )
                continue

            result = await quoter.quote_exact_in_async(pair.base, pair.quote, amount_in)
            if not result.ok or not result.amount_out_wei:
                await self._log_once(
                    f"dex_fwd_error_{key}_{pair.raw}", "warning",
                    f"{quoter.name} forward quote failed",
                    {"dex": quoter.name, "pair": pair.raw, "error": result.error},
                )
                continue

            price = price_from_amounts(
                amount_in, result.amount_out_wei, pair.base.decimals, pair.quote.decimals,
            )
            quotes.append(PriceQuote(
                dex=quoter.name, amount_in=amount_in,
                amount_out=result.amount_out_wei, price=price,
            ))
        return quotes

    async def _collect_reverse_quotes(
        self, pair, amount_in_quote: int, dex_list: List[str]
    ) -> List[PriceQuote]:
        """Quote quote→base on every DEX (including the same DEX as the forward leg).

        No supports_pair check here — we just try and skip on failure, since most
        quoters handle the reverse direction for the same pool without needing a
        separate check.
        """
        quotes: List[PriceQuote] = []
        for dex_name in dex_list:
            if self._stop_event.is_set():
                break
            key = normalize_dex_name(canonical_dex_name(dex_name))
            quoter = self.dex_quoters.get(key)
            if not quoter:
                continue

            result = await quoter.quote_exact_in_async(pair.quote, pair.base, amount_in_quote)
            if not result.ok or not result.amount_out_wei:
                continue

            price = price_from_amounts(
                amount_in_quote, result.amount_out_wei, pair.quote.decimals, pair.base.decimals,
            )
            quotes.append(PriceQuote(
                dex=quoter.name, amount_in=amount_in_quote,
                amount_out=result.amount_out_wei, price=price,
            ))
        return quotes

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

    async def _estimate_fees_in_base(
        self,
        base_token: Token,
        gas_price_wei: int,
        gas_used: int,
        dex_list: List[str],
    ) -> Decimal:
        """Return gas cost in base-token human-readable units (not wei)."""
        gas_cost_eth = Decimal(gas_price_wei) * Decimal(gas_used) / Decimal(10**18)
        if base_token.symbol in ("WETH", "ETH"):
            return gas_cost_eth

        # Convert ETH gas cost into base-token units via any available DEX.
        eth_token = get_token("WETH")
        amount_in = to_wei(Decimal("1"), eth_token.decimals)
        for dex_name in dex_list:
            key = normalize_dex_name(canonical_dex_name(dex_name))
            quoter = self.dex_quoters.get(key)
            if not quoter:
                continue
            result = await quoter.quote_exact_in_async(eth_token, base_token, amount_in)
            if result.ok and result.amount_out_wei:
                price = price_from_amounts(
                    amount_in, result.amount_out_wei,
                    eth_token.decimals, base_token.decimals,
                )
                return gas_cost_eth * price

        await self._log_once(
            f"fees_missing_{base_token.symbol}",
            "warning",
            "Unable to estimate gas fees in base token",
            {"base": base_token.symbol},
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
                    "fees": opp.fees_base,
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
