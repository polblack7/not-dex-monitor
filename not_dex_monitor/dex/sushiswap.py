from __future__ import annotations

from web3 import Web3

from .addresses import MAINNET_ADDRESSES
from .uniswap_v2 import UniswapV2LikeAdapter


class SushiSwapAdapter(UniswapV2LikeAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(
            w3,
            name="SushiSwap",
            router_address=MAINNET_ADDRESSES.sushiswap.router,
            factory_address=MAINNET_ADDRESSES.sushiswap.factory,
            venue="sushiswap",
            gas_estimate=180_000,
        )
