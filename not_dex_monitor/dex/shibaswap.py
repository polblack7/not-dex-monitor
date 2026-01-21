from __future__ import annotations

from web3 import Web3

from .addresses import MAINNET_ADDRESSES
from .uniswap_v2 import UniswapV2LikeAdapter


class ShibaSwapAdapter(UniswapV2LikeAdapter):
    def __init__(self, w3: Web3) -> None:
        super().__init__(
            w3,
            name="ShibaSwap",
            router_address=MAINNET_ADDRESSES.shibaswap.router,
            factory_address=MAINNET_ADDRESSES.shibaswap.factory,
            venue="shibaswap",
            gas_estimate=180_000,
        )
