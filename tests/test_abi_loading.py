import json

from web3 import Web3

from not_dex_monitor.dex.abi import list_abi_files


def test_abi_json_loads_and_contract_instantiates() -> None:
    w3 = Web3(Web3.HTTPProvider("http://localhost:8545"))
    dummy_address = "0x0000000000000000000000000000000000000001"
    for path in list_abi_files():
        data = json.loads(path.read_text())
        assert isinstance(data, list)
        contract = w3.eth.contract(address=dummy_address, abi=data)
        assert contract is not None
