from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from .dex import canonical_dex_name, normalize_dex_name
from .dex.addresses import (
    BALANCER_POOL_ADDRESSES,
    DEX_ROUTER_CONFIG,
    MAINNET_ADDRESSES,
    ZERO_ADDRESS,
)
from .dex.abi import load_abi
from .dex.fluid import FLUID_POOLS
from .models import Opportunity, Settings
from .tokens import parse_pair, Token
from .quote_math import to_wei
from .util.logging import get_logger


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Key derivation (vendored -- avoids depending on the backend package)
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

_V3_SWAP_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
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
    "uniswap v2": MAINNET_ADDRESSES.uniswap_v2.router,
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

    Resolution order:
    1. ``FLASH_LOAN_ABI_PATH`` env var (used by the Docker monitor container
       which mounts ``not-bot/artifacts`` at a fixed location).
    2. Walk up from this file looking for a sibling ``not-bot/`` directory
       so the function still works for local development.
    """
    env_path = os.getenv("FLASH_LOAN_ABI_PATH", "").strip()
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate

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


# DEX type IDs mirror FlashLoan.sol constants — keep in sync.
_DEX_TYPE_V2            = 0
_DEX_TYPE_V3            = 1
_DEX_TYPE_CURVE_STABLE  = 2
_DEX_TYPE_CURVE_CRYPTO  = 3
_DEX_TYPE_BALANCER_V2   = 4
_DEX_TYPE_FLUID         = 5

# Present in Curve stable pool bytecode; absent in crypto pools.
_CURVE_STABLE_EXCHANGE_SELECTOR = keccak(text="exchange(int128,int128,uint256,uint256)")[:4]

_DEX_V2_NAMES = {"uniswap v2", "sushiswap", "shibaswap"}


@dataclass(frozen=True)
class LegParams:
    dex_type: int
    target: str
    extra_data: bytes


def build_leg_params(
    w3: Web3,
    dex_name: str,
    token_in: Token,
    token_out: Token,
) -> Optional[LegParams]:
    """Return None for DEXes without an on-chain executor path (quoting-only)."""
    canonical = canonical_dex_name(normalize_dex_name(dex_name))

    if canonical in _DEX_V2_NAMES:
        cfg = DEX_ROUTER_CONFIG.get(canonical)
        if not cfg:
            return None
        return LegParams(_DEX_TYPE_V2, w3.to_checksum_address(cfg["router"]), b"")

    if canonical == "uniswap v3":
        cfg = DEX_ROUTER_CONFIG.get(canonical)
        if not cfg:
            return None
        fee = int(cfg.get("fee", 3000))
        return LegParams(
            _DEX_TYPE_V3,
            w3.to_checksum_address(cfg["router"]),
            abi_encode(["uint24"], [fee]),
        )

    if canonical == "curve":
        return _build_curve_leg(w3, token_in, token_out)

    if canonical == "balancer v2":
        return _build_balancer_v2_leg(w3, token_in, token_out)

    if canonical == "fluid dex":
        return _build_fluid_leg(token_in, token_out)

    return None


def _build_curve_leg(w3: Web3, token_in: Token, token_out: Token) -> Optional[LegParams]:
    addr_provider = w3.eth.contract(
        address=w3.to_checksum_address(MAINNET_ADDRESSES.curve.address_provider),
        abi=load_abi("curve", "address_provider"),
    )
    registry_addr: Optional[str] = None
    for getter in (
        lambda: addr_provider.functions.get_address(7).call(),
        lambda: addr_provider.functions.get_address(0).call(),
        lambda: addr_provider.functions.get_registry().call(),
    ):
        try:
            addr = getter()
            if addr and addr != ZERO_ADDRESS:
                registry_addr = addr
                break
        except Exception:  # noqa: BLE001
            continue
    if not registry_addr:
        return None

    registry = w3.eth.contract(
        address=w3.to_checksum_address(registry_addr),
        abi=load_abi("curve", "registry"),
    )
    token_in_addr  = w3.to_checksum_address(token_in.address)
    token_out_addr = w3.to_checksum_address(token_out.address)
    try:
        pool = registry.functions.find_pool_for_coins(token_in_addr, token_out_addr).call()
    except Exception:  # noqa: BLE001
        pool = None
    if not pool or pool == ZERO_ADDRESS:
        return None

    try:
        i, j, is_underlying = registry.functions.get_coin_indices(
            w3.to_checksum_address(pool), token_in_addr, token_out_addr,
        ).call()
    except Exception:  # noqa: BLE001
        return None
    if bool(is_underlying):
        # FlashLoan.sol calls plain `exchange`, not `exchange_underlying`.
        return None

    try:
        code = bytes(w3.eth.get_code(w3.to_checksum_address(pool)))
    except Exception:  # noqa: BLE001
        code = b""

    if _CURVE_STABLE_EXCHANGE_SELECTOR in code:
        return LegParams(
            _DEX_TYPE_CURVE_STABLE,
            w3.to_checksum_address(pool),
            abi_encode(["int128", "int128"], [int(i), int(j)]),
        )
    return LegParams(
        _DEX_TYPE_CURVE_CRYPTO,
        w3.to_checksum_address(pool),
        abi_encode(["uint256", "uint256"], [int(i), int(j)]),
    )


def _build_balancer_v2_leg(w3: Web3, token_in: Token, token_out: Token) -> Optional[LegParams]:
    key = (token_in.symbol.upper(), token_out.symbol.upper())
    alt = (key[1], key[0])
    pool_list = BALANCER_POOL_ADDRESSES.get(key) or BALANCER_POOL_ADDRESSES.get(alt) or []
    if not pool_list:
        return None
    pool_addr = w3.to_checksum_address(pool_list[0])

    pool_id: Optional[bytes] = None
    for abi_name in ("weighted_pool", "stable_pool"):
        try:
            pool = w3.eth.contract(address=pool_addr, abi=load_abi("balancer_v2", abi_name))
            pool_id = bytes(pool.functions.getPoolId().call())
            if pool_id:
                break
        except Exception:  # noqa: BLE001
            continue
    if not pool_id:
        return None

    return LegParams(
        _DEX_TYPE_BALANCER_V2,
        w3.to_checksum_address(MAINNET_ADDRESSES.balancer_v2.vault),
        abi_encode(["bytes32"], [pool_id]),
    )


def _build_fluid_leg(token_in: Token, token_out: Token) -> Optional[LegParams]:
    key = tuple(sorted([token_in.symbol.upper(), token_out.symbol.upper()]))
    pool = FLUID_POOLS.get(key)
    if not pool:
        return None
    # zeroToOne = (token_in is token0); token0 is the smaller address.
    zero_to_one = token_in.address.lower() < token_out.address.lower()
    return LegParams(
        _DEX_TYPE_FLUID,
        Web3.to_checksum_address(pool),
        abi_encode(["bool"], [zero_to_one]),
    )


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
            "flash_loan_contract not configured in settings -- "
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

    pair = parse_pair(opp.pair)
    base_token: Token = pair.base
    quote_token: Token = pair.quote

    if opp.amount_in_wei > 0:
        amount_in_wei = opp.amount_in_wei
    else:
        amount_in_wei = to_wei(Decimal(str(settings.loan_limit)), base_token.decimals)

    buy_leg = build_leg_params(w3, opp.buy_dex, base_token, quote_token)
    if buy_leg is None:
        return {"success": False, "error": f"Quoting-only support for {opp.buy_dex} (no on-chain executor)"}
    sell_leg = build_leg_params(w3, opp.sell_dex, quote_token, base_token)
    if sell_leg is None:
        return {"success": False, "error": f"Quoting-only support for {opp.sell_dex} (no on-chain executor)"}

    min_profit_wei = int(amount_in_wei * settings.min_profit_pct / 100)
    token_out_addr = w3.to_checksum_address(quote_token.address)
    params = abi_encode(
        [
            "address",
            "(uint8,address,bytes)",
            "(uint8,address,bytes)",
            "uint256",
        ],
        [
            token_out_addr,
            (buy_leg.dex_type,  buy_leg.target,  buy_leg.extra_data),
            (sell_leg.dex_type, sell_leg.target, sell_leg.extra_data),
            min_profit_wei,
        ],
    )

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

    try:
        gas_est = await asyncio.to_thread(
            contract.functions.requestFlashLoan(asset_addr, amount_in_wei, params).estimate_gas,
            {"from": sender},
        )
        gas_limit = int(gas_est * 1.2)
    except Exception:
        # Worst-case path (Curve crypto + Balancer V2) needs ~730k; round up to 800k.
        gas_limit = 800_000

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
    # Legacy path only supports DEXes with a simple router (V2 family + V3).
    # Curve / Balancer V2 / Fluid require the flash-loan contract.
    buy_cfg  = _get_router_config(opp.buy_dex)
    sell_cfg = _get_router_config(opp.sell_dex)
    if not buy_cfg or not sell_cfg:
        unsupported = opp.buy_dex if not buy_cfg else opp.sell_dex
        return TxResult(
            success=False,
            tx_hash=None,
            profit=0.0,
            fees=0.0,
            error=f"Quoting-only support for {unsupported} (no on-chain executor without flash-loan contract)",
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
            buy_cfg, sell_cfg,
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
    buy_cfg: dict,
    sell_cfg: dict,
) -> dict:
    """Execute buy (base->quote) on buy_router then sell (quote->base) on sell_router."""
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

    total_gas_used = 0
    nonce = await asyncio.to_thread(lambda: w3.eth.get_transaction_count(sender))

    buy_router_addr = buy_cfg["router"]
    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, base_addr, buy_router_addr,
        amount_in, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    deadline = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    buy_tx = _build_swap_tx(
        w3, buy_cfg, base_addr, quote_addr, amount_in, sender, deadline, gas_price, nonce, chain_id,
    )
    buy_receipt = await _sign_and_send(w3, private_key, buy_tx)
    if buy_receipt["status"] != 1:
        return {"success": False, "error": "Buy swap reverted", "tx_hash": buy_receipt["transactionHash"].hex()}
    total_gas_used += buy_receipt["gasUsed"]
    nonce += 1

    quote_contract = w3.eth.contract(address=quote_addr, abi=_ERC20_APPROVE_ABI)
    quote_balance  = await asyncio.to_thread(quote_contract.functions.balanceOf(sender).call)

    sell_router_addr = sell_cfg["router"]
    nonce, gas_used = await _ensure_allowance(
        w3, private_key, sender, quote_addr, sell_router_addr,
        quote_balance, nonce, chain_id, gas_price,
    )
    total_gas_used += gas_used

    deadline = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["timestamp"] + 300)
    sell_tx = _build_swap_tx(
        w3, sell_cfg, quote_addr, base_addr, quote_balance, sender, deadline, gas_price, nonce, chain_id,
    )
    sell_receipt = await _sign_and_send(w3, private_key, sell_tx)
    total_gas_used += sell_receipt["gasUsed"]

    if sell_receipt["status"] != 1:
        return {"success": False, "error": "Sell swap reverted", "tx_hash": sell_receipt["transactionHash"].hex()}

    final_balance  = await asyncio.to_thread(base_contract.functions.balanceOf(sender).call)
    profit_wei = final_balance - balance
    profit = float(Decimal(profit_wei) / Decimal(10 ** base_token.decimals))
    fees   = float(Decimal(total_gas_used * gas_price) / Decimal(10 ** 18))

    return {"success": True, "profit": profit, "fees": fees, "tx_hash": sell_receipt["transactionHash"].hex()}


def _build_swap_tx(
    w3: Web3,
    cfg: dict,
    token_in: str,
    token_out: str,
    amount_in: int,
    recipient: str,
    deadline: int,
    gas_price: int,
    nonce: int,
    chain_id: int,
) -> dict:
    router_addr = w3.to_checksum_address(cfg["router"])
    tx_overrides = {"from": recipient, "gas": 300_000, "gasPrice": gas_price, "nonce": nonce, "chainId": chain_id}

    if cfg["dex_type"] == 0:
        router = w3.eth.contract(address=router_addr, abi=_SWAP_ABI)
        return router.functions.swapExactTokensForTokens(
            amount_in, 0, [token_in, token_out], recipient, deadline
        ).build_transaction(tx_overrides)
    else:
        router = w3.eth.contract(address=router_addr, abi=_V3_SWAP_ABI)
        params = (token_in, token_out, cfg["fee"], recipient, deadline, amount_in, 0, 0)
        return router.functions.exactInputSingle(params).build_transaction(tx_overrides)


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
