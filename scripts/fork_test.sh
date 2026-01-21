#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "${ETH_RPC_URL:-}" ]]; then
  echo "ETH_RPC_URL not set; skipping fork test."
  exit 0
fi

if ! command -v anvil >/dev/null 2>&1; then
  echo "anvil not found; skipping fork test."
  exit 0
fi

ANVIL_PORT="${ANVIL_PORT:-8545}"
ANVIL_LOG="${ANVIL_LOG:-/tmp/anvil.log}"

anvil --fork-url "$ETH_RPC_URL" --chain-id 1 --port "$ANVIL_PORT" --silent >"$ANVIL_LOG" 2>&1 &
ANVIL_PID=$!
trap 'kill "$ANVIL_PID" >/dev/null 2>&1 || true' EXIT

sleep 3

python -m not_dex_monitor.fork_sweep \
  --rpc-url "http://127.0.0.1:${ANVIL_PORT}" \
  --min-success "${DEX_FORK_MIN_SUCCESS:-6}"
