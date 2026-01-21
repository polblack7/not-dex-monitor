from __future__ import annotations

from typing import Dict

from web3 import Web3

from .balancer_v2 import BalancerV2Adapter
from .base import BaseDexAdapter
from .curve import CurveAdapter
from .dodo_v2 import DodoV2Adapter
from .kyberswap_elastic import KyberSwapElasticAdapter
from .oneinch import OneInchAdapter
from .shibaswap import ShibaSwapAdapter
from .sushiswap import SushiSwapAdapter
from .uniswap_v2 import UniswapV2Adapter
from .uniswap_v3 import UniswapV3Adapter
from .zeroex import ZeroExAdapter


def normalize_dex_name(name: str) -> str:
    return name.strip().lower()


_DEX_ALIASES = {
    "uniswap": "uniswap v2",
    "uniswapv2": "uniswap v2",
    "uniswap v2": "uniswap v2",
    "uniswap v3": "uniswap v3",
    "uniswapv3": "uniswap v3",
    "sushiswap": "sushiswap",
    "shibaswap": "shibaswap",
    "curve": "curve",
    "balancer": "balancer v2",
    "balancer v2": "balancer v2",
    "0x": "0x",
    "zeroex": "0x",
    "1inch": "1inch",
    "oneinch": "1inch",
    "kyberswap": "kyberswap elastic",
    "kyberswap elastic": "kyberswap elastic",
    "dodo": "dodo v2",
    "dodo v2": "dodo v2",
}


def canonical_dex_name(name: str) -> str:
    normalized = normalize_dex_name(name)
    return _DEX_ALIASES.get(normalized, normalized)


def create_quoters(w3: Web3) -> Dict[str, BaseDexAdapter]:
    adapters = {
        "uniswap v2": UniswapV2Adapter(w3),
        "sushiswap": SushiSwapAdapter(w3),
        "shibaswap": ShibaSwapAdapter(w3),
        "uniswap v3": UniswapV3Adapter(w3),
        "curve": CurveAdapter(w3),
        "balancer v2": BalancerV2Adapter(w3),
        "0x": ZeroExAdapter(w3),
        "1inch": OneInchAdapter(w3),
        "kyberswap elastic": KyberSwapElasticAdapter(w3),
        "dodo v2": DodoV2Adapter(w3),
    }
    return {normalize_dex_name(name): adapter for name, adapter in adapters.items()}
