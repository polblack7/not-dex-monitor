# not-dex-monitor

Multi-user Ethereum DEX arbitrage opportunity monitor (MVP). This service only scans and reports opportunities to the existing `not-mini-app` backend; it does **not** execute trades or flashloans.

## Features
- Multi-user supervisor: discovers active wallets via `/internal/active-users` and spawns per-wallet workers.
- Per-wallet settings pulled from `/settings` with JWT.
- Reports opportunities, status, logs, and op attempts via `/internal/event`.
- Best-effort quoting across 10+ Ethereum venues (mainnet): Uniswap V2, SushiSwap, ShibaSwap, Uniswap V3, Curve, Balancer V2, 0x, 1inch, KyberSwap Elastic, DODO V2.
- ABI registry stored under `not_dex_monitor/abi/<venue>/<contract>.json`.

## Requirements
- Python 3.10+
- Access to an Ethereum JSON-RPC endpoint

## Install
```
cd not-dex-monitor
pip install -r requirements.txt
```

## Environment
Required (monitor mode):
- `BACKEND_BASE_URL` (default `http://localhost:8000`)
- `INTERNAL_API_KEY`
- `ACCESS_TOKEN_MASTER` (used to login per wallet via `/auth/login` and fetch `/settings`)
- `ETH_RPC_URL`

Required (dry-run mode):
- `ETH_RPC_URL`

Optional:
- `CHAIN_ID` (default `1`)
- `ACTIVE_USERS_POLL_SEC` (default `7`)
- `JWT_CACHE_TTL_SEC` (default `600`)
- `EMIT_OPS_ON_OPPORTUNITY` (default `true`)
- `HTTP_TIMEOUT_SEC` (default `15`)
- `HTTP_MAX_RETRIES` (default `3`)
- `HTTP_BACKOFF_BASE_SEC` (default `0.5`)
- `DEX_FORK_MIN_SUCCESS` (default `6`, fork sweep threshold)

## Run
```
cd not-dex-monitor
python -m not_dex_monitor.main --backend-url http://localhost:8000 --poll-sec 7 --log-level INFO
```

## Dry-run
```
cd not-dex-monitor
python -m not_dex_monitor.main --dry-run --wallet 0xYourWallet --dry-run-iterations 3
```
Dry-run reads `not_dex_monitor/examples/settings.json`, skips backend auth, and prints opportunities.

## Fork test
```
cd not-dex-monitor
ETH_RPC_URL=... ./scripts/fork_test.sh
```
The script starts an Anvil mainnet fork and runs a short quote sweep. It skips if `ETH_RPC_URL` or `anvil` is missing.

## Tests
```
cd not-dex-monitor
pytest
```

## Adding a new venue
1) Add ABI JSON files under `not_dex_monitor/abi/<venue>/<contract>.json`.
2) Add Ethereum mainnet addresses in `not_dex_monitor/dex/addresses.py`.
3) Implement a new adapter in `not_dex_monitor/dex/<venue>.py` with `supports_pair` and `quote_exact_in`.
4) Register the adapter in `not_dex_monitor/dex/__init__.py`.
5) Add the venue to `not_dex_monitor/examples/settings.json` and extend tests if needed.

## Notes
- `ACCESS_TOKEN_MASTER` is intended for dev/MVP only; it may rotate stored access tokens on login.
- Some venues (0x, 1inch) degrade gracefully when offchain RFQ is required and emit log events.
- Balancer/DODO pool lists are minimal and may need updates in `not_dex_monitor/dex/addresses.py`.
- Gas fee estimation uses a simple on-chain WETH->quote price lookup when possible.
- The monitor loads env vars from `not-dex-monitor/.env` and `not-dex-monitor/not_dex_monitor/.env` if present.
