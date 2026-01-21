import os
import shutil
import subprocess
import time

import pytest
from web3 import Web3

from not_dex_monitor.fork_sweep import assert_min_success, sweep_quotes


@pytest.mark.integration
def test_fork_sweep_with_anvil() -> None:
    rpc_url = os.getenv("ETH_RPC_URL")
    if not rpc_url:
        pytest.skip("ETH_RPC_URL not set")
    if not shutil.which("anvil"):
        pytest.skip("anvil not installed")

    port = 8545
    process = subprocess.Popen(
        [
            "anvil",
            "--fork-url",
            rpc_url,
            "--chain-id",
            "1",
            "--port",
            str(port),
            "--silent",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
        results = sweep_quotes(w3, ["WETH/USDC", "WETH/USDT", "WBTC/WETH"])
        assert_min_success(results, "WETH/USDC", int(os.getenv("DEX_FORK_MIN_SUCCESS", "6")))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
