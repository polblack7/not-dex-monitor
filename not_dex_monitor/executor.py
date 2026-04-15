from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from eth_abi import encode as abi_encode
from web3 import Web3

from .dex import canonical_dex_name, normalize_dex_name
from .dex.addresses import DEX_ROUTER_CONFIG, MAINNET_ADDRESSES
from .dex.abi import load_abi
from .models import Opportunity, Settings
from .tokens import parse_pair, Token
from .quote_math import to_wei
from .util.logging import get_logger


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Key derivation (vendored — avoids depending on the backend package)
# ---------------------------------------------------------------------------

import base64
import hashlib
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


def _derive_key(master_key: str, wallet_address: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(wallet_address.lower().encode()).digest(),
        info=b"onearb-wallet-key",
    )
    return base64.urlsafe_b64encode(hkdf.derive(master_key.encode()))


def decrypt_private_key(ciphertext: str, wallet_address: str, master_key: str) -> str:
    fernet = Fernet(_derive_key(master_key, wallet_address))
    return fernet.decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TxResult:
    success: bool
    tx_hash: Optional[str]
    profit: float
    fees: float
    error: Optional[str]
    exec_time_ms: int


# ---------------------------------------------------------------------------
# ABI fragments used by the legacy two-swap fallback path
# ---------------------------------------------------------------------------

_SWAP_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Legacy V2-router map (used by the fallback path only)
_V2_ROUTERS = {
    "uniswap_v2": MAINNET_ADDRESSES.uniswap_v2.router,
    "sushiswap":  MAINNET_ADDRESSES.sushiswap.router,
    "shibaswap":  MAINNET_ADDRESSES.shibaswap.router,
}


def _get_router_address(dex_name: str) -> Optional[str]:
    key = normalize_dex_name(canonical_dex_name(dex_name))
    return _V2_ROUTERS.get(key)


# ---------------------------------------------------------------------------
# Flash-loan ABI loading
# ---------------------------------------------------------------------------

def _default_flashloan_abi_path() -> Optional[Path]:
    """Return the best-guess path to the compiled FlashLoan artifact, or None.

    Walk up from this file looking for a sibling ``not-bot/`` directory so the
    function works both locally (repo root 3-4 levels up) and in Docker (where
    the monitor may be the only thing copied into the image).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "not-bot" / "artifacts" / "contracts" / "FlashLoan.sol" / "FlashLoan.json"
        if candidate.exists():
            return candidate
    return None


def _load_flashloan_abi(abi_path: str) -> list:
    """Load the FlashLoan contract ABI from the compiled Hardhat artifact."""
    if abi_path:
        path = Path(abi_path)
    else:
        path = _default_flashloan_abi_path()
        if path is None:
            raise FileNotFoundError(
                "FlashLoan ABI not found. Either set flash_loan_contract_abi_path in settings "
                "or run `npx hardhat compile` in not-bot/ and make the artifacts available."
            )
    if not path.exists():
        raise FileNotFoundError(f"FlashLoan ABI not found at {path}.")
    artifact = json.loads(path.read_text())
    # Hardhat artifacts wrap the ABI in an "abi" key
    return artifact["abi"] if isinstance(artifact, dict) else artifact


# ---------------------------------------------------------------------------
# DEX router config lookup
# ---------------------------------------------------------------------------

def _get_router_config(dex_name: str) -> Optional[dict]:
    """Return the DEX_ROUTER_CONFIG entry for a given DEX name (handles aliases)."""
    canonical = canonical_dex_name(normalize_dex_name(dex_name))
    return DEX_ROUTER_CONFIG.get(canonical)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_arb(
    w3: Web3,
    encrypted_key: str,
    master_key: str,
    wallet_address: str,
    opp: Opportunity,
    settings: Settings,
) -> TxResult:
    """Execute an arbitrage opportunity.

    If *settings.flash_loan_contract* is set the trade is executed as a single
    Aave V3 flash loan that performs both swap legs atomically on-chain.

    If the contract address is absent (empty / None) the legacy two-swap flow
    is used so that existing behaviour is preserved.
    """
    start = time.monotonic()

    # ------------------------------------------------------------------
    # Fast-path: fall back to legacy flow when no contract is configured
    # ------------------------------------------------------------------
    if not settings.flash_loan_contract:
        logger.warning(
            "flash_loan_contract not configured in settings — "
            "falling back to legacy two-swap flow for %s",
            opp.pair,
        )
        return await _execute_legacy(w3, encrypted_key, master_key, wallet_address, opp, settings, start)

    # ------------------------------------------------------------------
    # Flash-loan path
    # ------------------------------------------------------------------
    try:
        private_key = decrypt_private_key(encrypted_key, wallet_address, master_key)
    except Exception as exc:
        return TxResult(
            success=False, tx_hash=None, profit=0.0, fees=0.0,
            error=f"Key decryption failed: {exc}",
            exec_time_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        result = await _execute_flash_loan(w3, private_key, opp, settings)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    finally:
        del private_key

    elapsed = int((time.monotonic() - start) * 1000)
    return TxResult(
        success=result.get("success", False),
        tx_hash=result.get("tx_hash"),
        profit=result.get("profit", 0.0),
        fees=result.get("fees", 0.0),
        error=result.get("error"),
        exec_time_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Flash-loan execution
# ---------------------------------------------------------------------------

async def _execute_flash_loan(
    w3: Web3,
    private_key: str,
    opp: Opportunity,
    settings: Settings,
) -> dict:
    """Build and broadcast a single requestFlashLoan transaction."""

    # 1. Resolve tokens from pair string
    pair = parse_pair(opp.pair)
    base_token: Token = pair.base   # asset to borrow (tokenIn)
    quote_token: Token = pair.quote  # intermediate token (tokenOut)

    # 2. Determine flash loan amount
    if opp.amount_in_wei > 0:
        amount_in_wei = opp.amount_in_wei
    else:
        amount_in_wei = to_wei(Decimal(str(settings.loan_limit)), base_token.decimals)

    # 3. Resolve DEX router configs
    buy_cfg = _get_router_config(opp.buy_dex)
    sell_cfg = _get_router_config(opp.sell_dex)
    if not buy_cfg:
        return {"success": False, "error": f"No router config for buy DEX: {opp.buy_dex}"}
    if not sell_cfg:
        return {"success": False, "error": f"No router config for sell DEX: {opp.sell_dex}"}

    buy_router  = w3.to_checksum_address(buy_cfg["router"])
    sell_router = w3.to_checksum_address(sell_cfg["router"])
    buy_dex_type  = buy_cfg["dex_type"]
    sell_dex_type = sell_cfg["dex_type"]
    buy_fee   = buy_cfg["fee"]
    sell_fee  = sell_cfg["fee"]

    # 4. Compute minProfit in wei
    min_profit_wei = int(amount_in_wei * settings.min_profit_pct / 100)

    # 5. ABI-encode params blob
    token_out_addr = w3.to_checksum_address(quote_token.address)
    params = abi_encode(
        ["address", "uint8", "address", "uint24", "uint8", "address", "uint24", "uint256"],
        [
            token_out_addr,
            buy_dex_type,  buy_router,  buy_fee,
            sell_dex_type, sell_router, sell_fee,
            min_profit_wei,
        ],
    )

    # 6. Load FlashLoan contract
    abi = _load_flashloan_abi(settings.flash_loan_contract_abi_path)
    contract_addr = w3.to_checksum_address(settings.flash_loan_contract)
    contract = w3.eth.contract(address=contract_addr, abi=abi)

    asset_addr = w3.to_checksum_address(base_token.address)
    account = w3.eth.account.from_key(private_key)
    sender  = account.address

    chain_id  = await asyncio.to_thread(lambda: w3.eth.chain_id)
    gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
    bumped_gas_price = int(gas_price * 1.1)
    nonce = await asyncio.to_thread(lambda: w3.eth.get_transaction_count(sender))

    # 7. Estimate gas with 20 % buffer
    try:
        gas_est = await asyncio.to_thread(
            contract.functions.requestFlashLoan(asset_addr, amount_in_wei, params).estimate_gas,
            {"from": sender},
        )
        gas_limit = int(gas_est * 1.2)
    except Exception:
        gas_limit = 500_000  # conservative fallback

    # 8. Build transaction
    tx = contract.functions.requestFlashLoan(asset_addr, amount_in_wei, params).build_transaction(
        {
            "from":     sender,
            "nonce":    nonce,
            "gasPrice": bumped_gas_price,
            "gas":      gas_limit,
            "chainId":  chain_id,
        }
    )

    # 9–10. Sign and broadcast
    signed   = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash  = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()

    # 11. Wait for receipt
    receipt  = await asyncio.to_thread(
        w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
    )
    if receipt["status"] != 1:
        gas_cost = receipt["gasUsed"] * bumped_gas_price
        return {
            "success": False,
            "tx_hash": tx_hash_hex,
            "profit":  0.0,
            "fees":    float(Decimal(gas_cost) / Decimal(10 ** 18)),
            "error":   "requestFlashLoan reverted",
        }

    # 12. Parse ArbitrageExecuted event to get actual profit
    actual_profit_wei = 0
    try:
        arb_events = contract.events.ArbitrageExecuted().process_receipt(receipt)
        if arb_events:
            actual_profit_wei = arb_events[0]["args"]["profit"]
    except Exception as exc:
        logger.warning("Could not parse ArbitrageExecuted event: %s", exc)

    gas_cost_wei = receipt["gasUsed"] * bumped_gas_price
    actual_profit = float(Decimal(actual_profit_wei) / Decimal(10 ** base_token.decimals))
    fees = float(Decimal(gas_cost_wei) / Decimal(10 ** 18))

    logger.info(
        "Flash-loan arb executed: pair=%s buy=%s sell=%s profit=%.6f %s fees=%.6f ETH tx=%s",
        opp.pair, opp.buy_dex, opp.sell_dex,
        actual_profit, base_token.symbol, fees, tx_hash_hex,
    )

    return {
        "success": True,
        "tx_hash": tx_hash_hex,
        "profit":  actual_profit,
        "fees":    fees,
    }


# ---------------------------------------------------------------------------
# Legacy two-swap flow (preserved for backwards compatibility)
# ---------------------------------------------------------------------------

async def _execute_legacy(
    w3: Web3,
    encrypted_key: str,
    master_key: str,
    wallet_address: str,
    opp: Opportunity,
    settings: Settings,
    start: float,
) -> TxResult:
    buy_router_addr  = _get_router_address(opp.buy_dex)
    sell_router_addr = _get_router_address(opp.sell_dex)
    if not buy_router_addr or not sell_router_addr:
        return TxResult(
            success=False,
            tx_hash=None,
            profit=0.0,
            fees=0.0,
            error=f"Execution not supported for {opp.buy_dex}->{opp.sell_dex} (only V2-compatible routers)",
            exec_time_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        private_key = decrypt_private_key(encrypted_key, wallet_address, master_key)
    except Exception as exc:
        return TxResult(
            success=False, tx_hash=None, profit=0.0, fees=0.0,
            error=f"Key decryption failed: {exc}",
            exec_time_ms=int((time.monotonic() - start) * 1000),
        )

    try:
        pair = parse_pair(opp.pair)
    except ValueError as exc:
        del private_key
        return TxResult(
            success=False, tx_hash=None, profit=0.0, fees=0.0,
            error=str(exc),
            exec_time_ms=int((time.monotonic() - start) * 1000),
        )

    base_amount = Decimal(str(settings.loan_limit))
    amount_in   = to_wei(base_amount, pair.base.decimals)

    account = w3.eth.account.from_key(private_key)
    sender  = account.address

    try:
        result = await _execute_swap_pair(
            w3, private_key, sender, amount_in,
            pair.base, pair.quote,
            buy_router_addr, sell_router_addr,
        )
    finally:
        del private_key

    elapsed = int((time.monotonic() - start) * 1000)
    return TxResult(
        success=result["success"],
        tx_hash=result.get("tx_hash"),
        profit=result.get("profit", 0.0),
        fees=result.get("fees", 0.0),
        error=result.get("error"),
        exec_time_ms=elapsed,
    )


async def _execute_swap_pair(
    w3: Web3,
    private_key: str,
    sender: str,
    amount_in: int,
    base_token: Token,
    quote_token: Token,
    buy_router_addr: str,
    sell_router_addr: str,
) -> dict:
    """Execute buy (base→quote) on buy_router then sell (quote→base) on sell_router."""
    chain_id  = await asyncio.to_thread(lambda: w3.eth.chain_id)
    gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)

    base_addr  = w3.to_checksum_address(base_token.address)
    quote_addr = w3.to_checksum_address(quote_token.address)

    base_contract = w3.eth.contract(address=base_addr, abi=_ERC20_APPROVE_ABI)

    balance = await asyncio.to_thread(base_contract.functions.balanceOf(sender).call)
    if balance < amount_in:
        return {
            "success": False,
            "error": f"Insufficient {base_token.symbol} balance: have {balance}, need {amount_in}",
        }

    buy_router  = w3.eth.contract(address=w3.to_checksum_address(buy_router_addr), abi=_SWAP_ABI)
    total_gas_used = 0
    nonce = await asyncio.to_thread(lambda: w3.eth.get_transaction_count(sender))

    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, base_addr, buy_router_addr,
        amount_in, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    deadline = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    buy_tx = buy_router.functions.swapExactTokensForTokens(
        amount_in, 0, [base_addr, quote_addr], sender, deadline
    ).build_transaction({"from": sender, "gas": 300_000, "gasPrice": gas_price, "nonce": nonce, "chainId": chain_id})
    buy_receipt = await _sign_and_send(w3, private_key, buy_tx)
    if buy_receipt["status"] != 1:
        return {"success": False, "error": "Buy swap reverted", "tx_hash": buy_receipt["transactionHash"].hex()}
    total_gas_used += buy_receipt["gasUsed"]
    nonce += 1

    quote_contract = w3.eth.contract(address=quote_addr, abi=_ERC20_APPROVE_ABI)
    quote_balance  = await asyncio.to_thread(quote_contract.functions.balanceOf(sender).call)

    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, quote_addr, sell_router_addr,
        quote_balance, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    sell_router = w3.eth.contract(address=w3.to_checksum_address(sell_router_addr), abi=_SWAP_ABI)
    deadline    = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    sell_tx = sell_router.functions.swapExactTokensForTokens(
        quote_balance, 0, [quote_addr, base_addr], sender, deadline
    ).build_transaction({"from": sender, "gas": 300_000, "gasPrice": gas_price, "nonce": nonce, "chainId": chain_id})
    sell_receipt = await _sign_and_send(w3, private_key, sell_tx)
    total_gas_used += sell_receipt["gasUsed"]

    if sell_receipt["status"] != 1:
        return {"success": False, "error": "Sell swap reverted", "tx_hash": sell_receipt["transactionHash"].hex()}

    final_balance  = await asyncio.to_thread(base_contract.functions.balanceOf(sender).call)
    profit_wei = final_balance - balance
    profit = float(Decimal(profit_wei) / Decimal(10 ** base_token.decimals))
    fees   = float(Decimal(total_gas_used * gas_price) / Decimal(10 ** 18))

    return {"success": True, "profit": profit, "fees": fees, "tx_hash": sell_receipt["transactionHash"].hex()}


async def _ensure_allowance(
    w3: Web3,
    private_key: str,
    sender: str,
    token_addr: str,
    spender_addr: str,
    amount: int,
    nonce: int,
    chain_id: int,
    gas_price: int,
) -> tuple[int, int]:
    token    = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=_ERC20_APPROVE_ABI)
    spender  = w3.to_checksum_address(spender_addr)
    allowance = await asyncio.to_thread(token.functions.allowance(sender, spender).call)
    if allowance >= amount:
        return nonce, 0
    approve_tx = token.functions.approve(spender, amount).build_transaction(
        {"from": sender, "gas": 60_000, "gasPrice": gas_price, "nonce": nonce, "chainId": chain_id}
    )
    receipt = await _sign_and_send(w3, private_key, approve_tx)
    if receipt["status"] != 1:
        raise RuntimeError("ERC20 approve reverted")
    return nonce + 1, receipt["gasUsed"]


async def _sign_and_send(w3: Web3, private_key: str, tx: dict) -> dict:
    signed  = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
    receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
    return dict(receipt)
