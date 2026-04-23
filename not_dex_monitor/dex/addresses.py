from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class UniswapV2Addresses:
    router: str
    factory: str


@dataclass(frozen=True)
class UniswapV3Addresses:
    quoter_v2: str
    swap_router: str
    factory: str


@dataclass(frozen=True)
class CurveAddresses:
    address_provider: str
    registry: str


@dataclass(frozen=True)
class BalancerV2Addresses:
    vault: str


@dataclass(frozen=True)
class BalancerV3Addresses:
    vault: str
    router: str
    batch_router: str


@dataclass(frozen=True)
class ZeroExAddresses:
    exchange_proxy: str


@dataclass(frozen=True)
class OneInchAddresses:
    aggregation_router_v5: str
    offchain_oracle: str


@dataclass(frozen=True)
class KyberSwapElasticAddresses:
    quoter: str
    router: str
    factory: str


@dataclass(frozen=True)
class DodoV2Addresses:
    proxy: str


@dataclass(frozen=True)
class DexAddresses:
    uniswap_v2: UniswapV2Addresses
    sushiswap: UniswapV2Addresses
    shibaswap: UniswapV2Addresses
    uniswap_v3: UniswapV3Addresses
    curve: CurveAddresses
    balancer_v2: BalancerV2Addresses
    balancer_v3: BalancerV3Addresses
    zeroex: ZeroExAddresses
    oneinch: OneInchAddresses
    kyberswap_elastic: KyberSwapElasticAddresses
    dodo_v2: DodoV2Addresses


MAINNET_ADDRESSES = DexAddresses(
    uniswap_v2=UniswapV2Addresses(
        router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
    ),
    sushiswap=UniswapV2Addresses(
        router="0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        factory="0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
    ),
    shibaswap=UniswapV2Addresses(
        router="0x03f7724180AA6b939894B5Ca4314783B0b36b329",
        factory="0x115934131916C8b277DD010Ee02de363c09d037c",
    ),
    uniswap_v3=UniswapV3Addresses(
        quoter_v2="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        swap_router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
    ),
    curve=CurveAddresses(
        address_provider="0x0000000022D53366457F9d5E68Ec105046FC4383",
        registry="0x90E00ACe148ca3b23Ac1bC8C240C2a7Dd9c2d7f6",
    ),
    balancer_v2=BalancerV2Addresses(
        vault="0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    ),
    balancer_v3=BalancerV3Addresses(
        vault="0xbA1333333333a1BA1108E8412f11850A5C319bA9",
        router="0xAE563E3f8219521950555F5962419C8919758Ea2",
        batch_router="0x136f1EFcC3f8f88516B9E94110D56FDBfB1778d1",
    ),
    zeroex=ZeroExAddresses(
        exchange_proxy="0xdef1c0ded9bec7f1a1670819833240f027b25eff",
    ),
    oneinch=OneInchAddresses(
        aggregation_router_v5="0x1111111254EEB25477B68fb85Ed929f73A960582",
        offchain_oracle="0x07D91f5fb9Bf7798734C3f606dB065549F6893bb",
    ),
    kyberswap_elastic=KyberSwapElasticAddresses(
        quoter="0x0D125c15D54cA1F8a813C74A81aEe34ebB508C1f",
        router="0xF9c2b5746c946EF883ab2660BbbB1f10A5bdeAb4",
        factory="0x5F1dddbf348aC2fbe22a163e30F99F9ECE3DD50a",
    ),
    dodo_v2=DodoV2Addresses(
        proxy="0xa356867fDCEa8e71AEaF87805808803806231FdC",
    ),
)


# ---------------------------------------------------------------------------
# Flash-loan executor router lookup table
# ---------------------------------------------------------------------------
# Keys match the canonical DEX names returned by canonical_dex_name() in
# dex/__init__.py (lowercase, spaces preserved).
# dex_type: 0 = Uniswap V2-compatible, 1 = Uniswap V3-compatible
# fee: Uniswap V3 pool fee tier in bps (ignored for V2, set to 0)

DEX_ROUTER_CONFIG: Dict[str, dict] = {
    "uniswap v2": {
        "router": MAINNET_ADDRESSES.uniswap_v2.router,
        "dex_type": 0,
        "fee": 0,
    },
    "sushiswap": {
        "router": MAINNET_ADDRESSES.sushiswap.router,
        "dex_type": 0,
        "fee": 0,
    },
    "shibaswap": {
        "router": MAINNET_ADDRESSES.shibaswap.router,
        "dex_type": 0,
        "fee": 0,
    },
    "uniswap v3": {
        "router": MAINNET_ADDRESSES.uniswap_v3.swap_router,
        "dex_type": 1,
        "fee": 3000,  # default 0.3% tier; per-pair fees can override in future
    },
}


BALANCER_POOL_ADDRESSES: Dict[Tuple[str, str], List[str]] = {
    ("WETH", "USDC"): [
        "0x96646936b91d6b9d7d0c47c496afbf3d6ec7b6f8",
    ],
    ("WETH", "USDT"): [
        "0x3e5fa9518ea95c3e533eb377c001702a9aacaa32",
    ],
    ("WBTC", "WETH"): [
        "0xa6f548df93de924d73be7d25dc02554c6bd66db5",
    ],
}


# Balancer V3 pool addresses (pool contract address, not pool ID).
# V3 launched Dec 2024; add new pools here as liquidity grows.
BALANCER_V3_POOL_ADDRESSES: Dict[Tuple[str, str], List[str]] = {}


DODO_V2_POOL_ADDRESSES: Dict[Tuple[str, str], List[str]] = {
    ("WETH", "USDC"): [
        "0x75c23271661d9d143DCb617222BC4BEc783eff34",
    ],
    ("WETH", "USDT"): [
        "0x10c8c2d99b4b4f0f631c64bdc6f2d6a8cf9a34d6",
    ],
    ("WBTC", "WETH"): [
        "0x4f2f48b18f3c91df72f4a20d4e6b1a843e0f8f55",
    ],
}
