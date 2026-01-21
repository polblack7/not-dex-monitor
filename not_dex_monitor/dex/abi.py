from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import List


ABI_ROOT = Path(__file__).resolve().parent.parent / "abi"


def abi_path(venue: str, contract: str) -> Path:
    return ABI_ROOT / venue / f"{contract}.json"


@lru_cache(maxsize=None)
def load_abi(venue: str, contract: str) -> List[dict]:
    path = abi_path(venue, contract)
    if not path.exists():
        raise FileNotFoundError(f"Missing ABI for {venue}/{contract}: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"ABI at {path} must be a JSON list")
    return data


def list_abi_files() -> List[Path]:
    if not ABI_ROOT.exists():
        return []
    return sorted(path for path in ABI_ROOT.rglob("*.json") if path.is_file())
