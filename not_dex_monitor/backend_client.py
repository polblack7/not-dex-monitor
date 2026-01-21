from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp

from .util.retry import RetryableError, retry_async
from .util.logging import get_logger


logger = get_logger(__name__)


class BackendError(Exception):
    def __init__(self, message: str, *, code: Optional[str] = None, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class AuthError(BackendError):
    pass


class BackendClient:
    def __init__(
        self,
        *,
        base_url: str,
        internal_api_key: str,
        timeout_sec: int,
        max_retries: int,
        backoff_base_sec: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_api_key = internal_api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.max_retries = max_retries
        self.backoff_base_sec = backoff_base_sec
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "BackendClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"

        async def do_request() -> Any:
            try:
                async with session.request(method, url, headers=headers, json=payload) as resp:
                    if resp.status in {429} or resp.status >= 500:
                        retry_after = None
                        if "Retry-After" in resp.headers:
                            try:
                                retry_after = float(resp.headers["Retry-After"])
                            except ValueError:
                                retry_after = None
                        raise RetryableError(f"HTTP {resp.status}", retry_after=retry_after)

                    data: Any = None
                    text_body = None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001
                        text_body = await resp.text()

                    if resp.status == 401:
                        raise AuthError("Unauthorized", status=resp.status)
                    if resp.status >= 400:
                        raise BackendError(text_body or f"HTTP {resp.status}", status=resp.status)

                    if isinstance(data, dict) and "ok" in data:
                        if not data.get("ok"):
                            err = data.get("error", {}) if isinstance(data.get("error"), dict) else {}
                            raise BackendError(
                                err.get("message", "Backend error"),
                                code=err.get("code"),
                                status=resp.status,
                            )
                        return data.get("data")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise RetryableError(str(exc)) from exc

        return await retry_async(
            do_request,
            retries=self.max_retries,
            base_delay=self.backoff_base_sec,
        )

    async def get_active_users(self) -> List[str]:
        data = await self._request_json(
            "GET",
            "/internal/active-users",
            headers={"X-Internal-Key": self.internal_api_key},
        )
        return list(data or [])

    async def login(self, wallet_address: str, access_token: str) -> str:
        data = await self._request_json(
            "POST",
            "/auth/login",
            payload={"wallet_address": wallet_address, "access_token": access_token},
        )
        if not isinstance(data, dict) or "token" not in data:
            raise BackendError("Unexpected login response")
        return data["token"]

    async def get_settings(self, jwt: str) -> Dict[str, Any]:
        data = await self._request_json(
            "GET",
            "/settings",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        if not isinstance(data, dict):
            raise BackendError("Unexpected settings response")
        return data

    async def post_event(self, wallet_address: str, event_type: str, payload: Dict[str, Any]) -> None:
        await self._request_json(
            "POST",
            "/internal/event",
            headers={"X-Internal-Key": self.internal_api_key},
            payload={"wallet_address": wallet_address, "type": event_type, "payload": payload},
        )
