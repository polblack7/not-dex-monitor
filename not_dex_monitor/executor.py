from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from .dex import canonical_dex_name, normalize_dex_name
from .dex.addresses import MAINNET_ADDRESSES
from .dex.abi import load_abi
from .models import Opportunity, Settings
from .tokens import get_token, parse_pair, Token
from .quote_math import to_wei
from .util.logging import get_logger


logger = get_logger(__name__)

# Reuse the same crypto derivation used by the backend.
# We vendor the minimal HKDF logic so the monitor doesn't depend on the
# backend package – only on the `cryptography` library that is already
# transitively available via web3.
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


@dataclass(frozen=True)
class TxResult:
    success: bool
    tx_hash: Optional[str]
    profit: float
    fees: float
    error: Optional[str]
    exec_time_ms: int


# Uniswap V2-style router ABI fragment for swapExactTokensForTokens
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

# Map DEX names to their Uniswap-V2-compatible router addresses
_V2_ROUTERS = {
    "uniswap_v2": MAINNET_ADDRESSES.uniswap_v2.router,
    "sushiswap": MAINNET_ADDRESSES.sushiswap.router,
    "shibaswap": MAINNET_ADDRESSES.shibaswap.router,
}


def _get_router_address(dex_name: str) -> Optional[str]:
    key = normalize_dex_name(canonical_dex_name(dex_name))
    return _V2_ROUTERS.get(key)


async def execute_arb(
    w3: Web3,
    encrypted_key: str,
    master_key: str,
    wallet_address: str,
    opp: Opportunity,
    settings: Settings,
) -> TxResult:
    """Execute an arbitrage: buy on buy_dex, sell on sell_dex.

    Only Uniswap-V2-compatible routers are supported for execution.
    Returns a TxResult with profit/loss and tx details.
    """
    start = time.monotonic()

    buy_router_addr = _get_router_address(opp.buy_dex)
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

    # Decrypt private key
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

    # Enforce loan_limit
    base_amount = Decimal(str(settings.loan_limit))
    amount_in = to_wei(base_amount, pair.base.decimals)

    account = w3.eth.account.from_key(private_key)
    sender = account.address

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
    """Execute buy (base->quote) on buy_router then sell (quote->base) on sell_router."""
    chain_id = await asyncio.to_thread(lambda: w3.eth.chain_id)
    gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)

    base_addr = w3.to_checksum_address(base_token.address)
    quote_addr = w3.to_checksum_address(quote_token.address)

    base_contract = w3.eth.contract(address=base_addr, abi=_ERC20_APPROVE_ABI)

    # Check balance
    balance = await asyncio.to_thread(base_contract.functions.balanceOf(sender).call)
    if balance < amount_in:
        return {
            "success": False,
            "error": f"Insufficient {base_token.symbol} balance: have {balance}, need {amount_in}",
        }

    buy_router = w3.eth.contract(
        address=w3.to_checksum_address(buy_router_addr), abi=_SWAP_ABI
    )

    total_gas_used = 0
    nonce = await asyncio.to_thread(lambda: w3.eth.get_transaction_count(sender))

    # --- Approve base token for buy router ---
    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, base_addr, buy_router_addr,
        amount_in, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    # --- Buy: swap base -> quote on buy_dex ---
    deadline = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    buy_tx = buy_router.functions.swapExactTokensForTokens(
        amount_in, 0, [base_addr, quote_addr], sender, deadline
    ).build_transaction({
        "from": sender,
        "gas": 300_000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": chain_id,
    })
    buy_receipt = await _sign_and_send(w3, private_key, buy_tx)
    if buy_receipt["status"] != 1:
        return {"success": False, "error": "Buy swap reverted", "tx_hash": buy_receipt["transactionHash"].hex()}
    total_gas_used += buy_receipt["gasUsed"]
    nonce += 1

    # Get quote token amount received
    quote_contract = w3.eth.contract(address=quote_addr, abi=_ERC20_APPROVE_ABI)
    quote_balance = await asyncio.to_thread(quote_contract.functions.balanceOf(sender).call)

    # --- Approve quote token for sell router ---
    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, quote_addr, sell_router_addr,
        quote_balance, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    # --- Sell: swap quote -> base on sell_dex ---
    sell_router = w3.eth.contract(
        address=w3.to_checksum_address(sell_router_addr), abi=_SWAP_ABI
    )
    deadline = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    sell_tx = sell_router.functions.swapExactTokensForTokens(
        quote_balance, 0, [quote_addr, base_addr], sender, deadline
    ).build_transaction({
        "from": sender,
        "gas": 300_000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": chain_id,
    })
    sell_receipt = await _sign_and_send(w3, private_key, sell_tx)
    total_gas_used += sell_receipt["gasUsed"]

    if sell_receipt["status"] != 1:
        return {"success": False, "error": "Sell swap reverted", "tx_hash": sell_receipt["transactionHash"].hex()}

    # Compute profit
    final_balance = await asyncio.to_thread(base_contract.functions.balanceOf(sender).call)
    profit_wei = final_balance - balance
    profit = float(Decimal(profit_wei) / Decimal(10 ** base_token.decimals))
    fees = float(Decimal(total_gas_used * gas_price) / Decimal(10 ** 18))

    return {
        "success": True,
        "profit": profit,
        "fees": fees,
        "tx_hash": sell_receipt["transactionHash"].hex(),
    }


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
    """Approve spender if current allowance is insufficient. Returns (new_nonce, gas_used)."""
    token = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=_ERC20_APPROVE_ABI)
    spender = w3.to_checksum_address(spender_addr)
    allowance = await asyncio.to_thread(token.functions.allowance(sender, spender).call)
    if allowance >= amount:
        return nonce, 0

    approve_tx = token.functions.approve(spender, amount).build_transaction({
        "from": sender,
        "gas": 60_000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": chain_id,
    })
    receipt = await _sign_and_send(w3, private_key, approve_tx)
    if receipt["status"] != 1:
        raise RuntimeError("ERC20 approve reverted")
    return nonce + 1, receipt["gasUsed"]


async def _sign_and_send(w3: Web3, private_key: str, tx: dict) -> dict:
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
    receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
    return dict(receipt)
