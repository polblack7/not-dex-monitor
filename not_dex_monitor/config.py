from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class Config:
    backend_base_url: str
    internal_api_key: str
    access_token_master: Optional[str]
    eth_rpc_url: str
    chain_id: int
    active_users_poll_sec: int
    jwt_cache_ttl_sec: int
    emit_ops_on_opportunity: bool
    http_timeout_sec: int
    http_max_retries: int
    http_backoff_base_sec: float
    wallet_encryption_key: str

    @classmethod
    def from_env(
        cls,
        *,
        backend_base_url: Optional[str] = None,
        active_users_poll_sec: Optional[int] = None,
        require_backend: bool = True,
    ) -> "Config":
        _load_dotenv(_default_dotenv_paths())

        def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
            value = os.getenv(name)
            return value if value is not None else default

        def get_int(name: str, default: int) -> int:
            value = get_env(name)
            if value is None:
                return default
            return int(value)

        def get_bool(name: str, default: bool) -> bool:
            value = get_env(name)
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}

        backend_url = backend_base_url or get_env("BACKEND_BASE_URL", "http://localhost:8000")
        poll_sec = active_users_poll_sec if active_users_poll_sec is not None else get_int(
            "ACTIVE_USERS_POLL_SEC", 7
        )

        cfg = cls(
            backend_base_url=backend_url,
            internal_api_key=get_env("INTERNAL_API_KEY", "") or "",
            access_token_master=get_env("ACCESS_TOKEN_MASTER"),
            eth_rpc_url=get_env("ETH_RPC_URL", "") or "",
            chain_id=get_int("CHAIN_ID", 1),
            active_users_poll_sec=poll_sec,
            jwt_cache_ttl_sec=get_int("JWT_CACHE_TTL_SEC", 600),
            emit_ops_on_opportunity=get_bool("EMIT_OPS_ON_OPPORTUNITY", True),
            http_timeout_sec=get_int("HTTP_TIMEOUT_SEC", 15),
            http_max_retries=get_int("HTTP_MAX_RETRIES", 3),
            http_backoff_base_sec=float(get_env("HTTP_BACKOFF_BASE_SEC", "0.5") or "0.5"),
            wallet_encryption_key=get_env("WALLET_ENCRYPTION_KEY", "") or "",
        )
        cfg._validate(require_backend=require_backend)
        return cfg

    def _validate(self, *, require_backend: bool = True) -> None:
        missing = []
        if require_backend:
            if not self.internal_api_key:
                missing.append("INTERNAL_API_KEY")
            if not self.access_token_master:
                missing.append("ACCESS_TOKEN_MASTER")
        if not self.eth_rpc_url:
            missing.append("ETH_RPC_URL")
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")


def _default_dotenv_paths() -> Iterable[Path]:
    module_dir = Path(__file__).resolve().parent
    root_dir = module_dir.parent
    return (root_dir / ".env", module_dir / ".env")


def _load_dotenv(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
