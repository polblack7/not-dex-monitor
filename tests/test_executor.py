"""Unit tests for not_dex_monitor.executor.

All blockchain calls are mocked so no real node or private key is needed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from not_dex_monitor.executor import (
    TxResult,
    _get_router_config,
    execute_arb,
)
from not_dex_monitor.models import Opportunity, Settings


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# Minimal FlashLoan ABI that contains only what the executor touches
MINIMAL_ABI = [
    {
        "inputs": [
            {"name": "asset",  "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "params", "type": "bytes"},
        ],
        "name": "requestFlashLoan",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "tokenIn",        "type": "address"},
            {"indexed": True,  "name": "tokenOut",       "type": "address"},
            {"indexed": False, "name": "amountBorrowed", "type": "uint256"},
            {"indexed": False, "name": "profit",         "type": "uint256"},
        ],
        "name": "ArbitrageExecuted",
        "type": "event",
    },
]

WALLET_ADDR   = "0x1234567890123456789012345678901234567890"
CONTRACT_ADDR = "0xDeadBeefDeadBeefDeadBeefDeadBeefDeadBeef"
TX_HASH_HEX   = "0x" + "ab" * 32

# A working Fernet-encrypted dummy private key (32 zero bytes, base64)
# We patch decrypt_private_key so the actual value doesn't matter.
ENCRYPTED_KEY = "gAAAAA"
MASTER_KEY    = "secret"

SAMPLE_PRIVATE_KEY = "0x" + "a0" * 32


def _make_opp(**kwargs) -> Opportunity:
    defaults = dict(
        pair="WETH/USDC",
        buy_dex="uniswap v2",
        sell_dex="sushiswap",
        expected_profit_pct=0.5,
        liquidity_score=1.0,
        gas_price_gwei=30.0,
        route=["WETH", "USDC"],
        fees_quote=0.0,
        amount_in_wei=int(1e18),
    )
    defaults.update(kwargs)
    return Opportunity(**defaults)


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        flash_loan_contract=CONTRACT_ADDR,
        flash_loan_contract_abi_path="",
        min_profit_pct=0.3,
        loan_limit=1.0,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _make_receipt(*, status: int = 1, gas_used: int = 200_000, profit_wei: int = int(0.05e18)) -> dict:
    """Build a minimal tx receipt dict (mimics web3.py AttributeDict)."""
    return {
        "status": status,
        "gasUsed": gas_used,
        "transactionHash": bytes.fromhex(TX_HASH_HEX[2:]),
        "logs": [],
    }


def _make_w3_mock(
    *,
    chain_id: int = 1,
    gas_price: int = int(30e9),
    nonce: int = 5,
    gas_estimate: int = 300_000,
    receipt: dict | None = None,
) -> MagicMock:
    """Return a mock Web3 instance with common attributes pre-configured."""
    if receipt is None:
        receipt = _make_receipt()

    w3 = MagicMock()
    w3.eth.chain_id = chain_id
    w3.eth.gas_price = gas_price
    w3.eth.get_transaction_count.return_value = nonce
    w3.to_checksum_address.side_effect = lambda addr: addr  # identity

    # Contract mock
    fn_mock = MagicMock()
    fn_mock.estimate_gas.return_value = gas_estimate
    fn_mock.build_transaction.return_value = {"from": WALLET_ADDR, "nonce": nonce}

    contract_mock = MagicMock()
    contract_mock.functions.requestFlashLoan.return_value = fn_mock

    # ArbitrageExecuted event parsing
    arb_event_mock = MagicMock()
    arb_event_mock.process_receipt.return_value = [
        {"args": {"profit": int(0.05e18), "tokenIn": WALLET_ADDR, "tokenOut": WALLET_ADDR}}
    ]
    contract_mock.events.ArbitrageExecuted.return_value = arb_event_mock

    w3.eth.contract.return_value = contract_mock

    # account
    account_mock = MagicMock()
    account_mock.address = WALLET_ADDR
    w3.eth.account.from_key.return_value = account_mock
    signed_mock = MagicMock()
    signed_mock.raw_transaction = b"\x00" * 32
    w3.eth.account.sign_transaction.return_value = signed_mock
    w3.eth.send_raw_transaction.return_value = bytes.fromhex(TX_HASH_HEX[2:])
    w3.eth.wait_for_transaction_receipt.return_value = receipt

    return w3


# ---------------------------------------------------------------------------
# Router config lookup
# ---------------------------------------------------------------------------

class TestGetRouterConfig:
    def test_uniswap_v2_canonical(self):
        cfg = _get_router_config("uniswap v2")
        assert cfg is not None
        assert cfg["dex_type"] == 0

    def test_uniswap_v2_alias(self):
        # "uniswap" is a registered alias → resolves to "uniswap v2"
        assert _get_router_config("uniswap") is not None

    def test_sushiswap(self):
        cfg = _get_router_config("sushiswap")
        assert cfg is not None
        assert cfg["dex_type"] == 0

    def test_shibaswap(self):
        cfg = _get_router_config("shibaswap")
        assert cfg is not None
        assert cfg["dex_type"] == 0

    def test_uniswap_v3(self):
        cfg = _get_router_config("uniswap v3")
        assert cfg is not None
        assert cfg["dex_type"] == 1
        assert cfg["fee"] == 3000

    def test_unknown_dex_returns_none(self):
        assert _get_router_config("pancakeswap") is None


# ---------------------------------------------------------------------------
# execute_arb — flash-loan path
# ---------------------------------------------------------------------------

class TestExecuteArbFlashLoan:
    """Tests for execute_arb when flash_loan_contract is configured."""

    def _run(self, coro):
        return asyncio.run(coro)

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_success_returns_correct_txresult(self, mock_decrypt, mock_abi):
        opp      = _make_opp()
        settings = _make_settings()
        w3       = _make_w3_mock()

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        assert isinstance(result, TxResult)
        assert result.success is True
        # executor calls bytes.hex() which omits the 0x prefix
        assert result.tx_hash == TX_HASH_HEX.lstrip("0x")
        assert result.error is None
        assert result.profit >= 0.0
        assert result.fees >= 0.0
        assert result.exec_time_ms >= 0

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_requestFlashLoan_called_with_correct_asset_and_amount(self, mock_decrypt, mock_abi):
        opp      = _make_opp(amount_in_wei=int(2e18))
        settings = _make_settings()
        w3       = _make_w3_mock()

        self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        contract_mock = w3.eth.contract.return_value
        # The function was called with asset=WETH address and amount=2e18
        call_args = contract_mock.functions.requestFlashLoan.call_args
        assert call_args is not None
        asset, amount, _ = call_args[0]
        from not_dex_monitor.tokens import get_token
        assert asset == get_token("WETH").address
        assert amount == int(2e18)

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_params_abi_encoding_includes_routers(self, mock_decrypt, mock_abi):
        """Params bytes must be non-empty and contain the buy/sell router addresses."""
        from not_dex_monitor.dex.addresses import DEX_ROUTER_CONFIG
        opp      = _make_opp(buy_dex="uniswap v2", sell_dex="sushiswap")
        settings = _make_settings()
        w3       = _make_w3_mock()

        self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        contract_mock = w3.eth.contract.return_value
        call_args = contract_mock.functions.requestFlashLoan.call_args
        _, _, params_bytes = call_args[0]
        assert isinstance(params_bytes, bytes)
        assert len(params_bytes) > 0

        # Router addresses must appear in the encoded params (without 0x prefix, lowercase)
        buy_router  = DEX_ROUTER_CONFIG["uniswap v2"]["router"].lower()[2:]
        sell_router = DEX_ROUTER_CONFIG["sushiswap"]["router"].lower()[2:]
        params_hex = params_bytes.hex()
        assert buy_router  in params_hex
        assert sell_router in params_hex

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_gas_price_bumped_by_10_percent(self, mock_decrypt, mock_abi):
        base_gas_price = int(30e9)
        opp      = _make_opp()
        settings = _make_settings()
        w3       = _make_w3_mock(gas_price=base_gas_price)

        self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        # gasPrice is passed to build_transaction, not directly to sign_transaction
        fn_mock   = w3.eth.contract.return_value.functions.requestFlashLoan.return_value
        build_arg = fn_mock.build_transaction.call_args[0][0]
        assert build_arg["gasPrice"] == int(base_gas_price * 1.1)

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_reverted_tx_returns_failure(self, mock_decrypt, mock_abi):
        failed_receipt = _make_receipt(status=0)
        opp      = _make_opp()
        settings = _make_settings()
        w3       = _make_w3_mock(receipt=failed_receipt)

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        assert result.success is False
        assert result.tx_hash == TX_HASH_HEX.lstrip("0x")
        assert "reverted" in (result.error or "")

    @patch("not_dex_monitor.executor.decrypt_private_key", side_effect=Exception("bad key"))
    def test_decryption_failure_returns_error(self, mock_decrypt):
        opp      = _make_opp()
        settings = _make_settings()
        w3       = _make_w3_mock()

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        assert result.success is False
        assert "Key decryption failed" in (result.error or "")
        assert result.tx_hash is None

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_unknown_buy_dex_returns_error(self, mock_decrypt, mock_abi):
        opp      = _make_opp(buy_dex="pancakeswap")
        settings = _make_settings()
        w3       = _make_w3_mock()

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        assert result.success is False
        assert "pancakeswap" in (result.error or "")

    @patch("not_dex_monitor.executor._load_flashloan_abi", return_value=MINIMAL_ABI)
    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_amount_in_wei_derived_from_loan_limit_when_zero(self, mock_decrypt, mock_abi):
        """If opp.amount_in_wei == 0, derive amount from settings.loan_limit."""
        opp      = _make_opp(amount_in_wei=0)
        settings = _make_settings(loan_limit=2.0)  # 2 WETH = 2e18 wei
        w3       = _make_w3_mock()

        self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        contract_mock = w3.eth.contract.return_value
        _, amount, _ = contract_mock.functions.requestFlashLoan.call_args[0]
        assert amount == int(2e18)


# ---------------------------------------------------------------------------
# execute_arb — legacy fallback path
# ---------------------------------------------------------------------------

class TestExecuteArbLegacyFallback:
    """When flash_loan_contract is empty, the old two-swap flow is used."""

    def _run(self, coro):
        return asyncio.run(coro)

    @patch("not_dex_monitor.executor.decrypt_private_key", return_value=SAMPLE_PRIVATE_KEY)
    def test_falls_back_when_no_contract_configured(self, mock_decrypt):
        settings = _make_settings(flash_loan_contract="")
        opp      = _make_opp(buy_dex="uniswap v2", sell_dex="sushiswap")
        w3       = _make_w3_mock()

        # Legacy path requires ERC-20 balance check — mock it
        erc20_mock = MagicMock()
        erc20_mock.functions.balanceOf.return_value.call.return_value = int(10e18)
        erc20_mock.functions.allowance.return_value.call.return_value = int(10e18)
        w3.eth.contract.return_value = erc20_mock

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        # Should not crash; success depends on mock receipt status
        assert isinstance(result, TxResult)
        # requestFlashLoan should NOT have been called (legacy path uses swapExactTokensForTokens)
        # This is validated by checking no requestFlashLoan in the contract calls
        for c in w3.eth.contract.call_args_list:
            # no call should be to the flash loan contract address
            if CONTRACT_ADDR in str(c):
                pytest.fail("requestFlashLoan should not be called in legacy path")

    def test_falls_back_for_unsupported_dex_returns_error(self):
        settings = _make_settings(flash_loan_contract="")
        opp      = _make_opp(buy_dex="curve", sell_dex="balancer v2")
        w3       = _make_w3_mock()

        result = self._run(execute_arb(w3, ENCRYPTED_KEY, MASTER_KEY, WALLET_ADDR, opp, settings))

        assert result.success is False
        assert result.tx_hash is None
        assert "only V2-compatible routers" in (result.error or "")
