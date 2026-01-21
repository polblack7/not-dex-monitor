from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional, TypeVar


T = TypeVar("T")


class RetryableError(Exception):
    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _default_retry_on(exc: Exception) -> bool:
    return isinstance(exc, RetryableError)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    retries: int,
    base_delay: float,
    max_delay: float = 10.0,
    retry_on: Callable[[Exception], bool] = _default_retry_on,
) -> T:
    attempt = 0
    while True:
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001
            if not retry_on(exc) or attempt >= retries:
                raise
            delay = base_delay * (2 ** attempt)
            if isinstance(exc, RetryableError) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            delay = min(delay, max_delay)
            jitter = delay * random.uniform(0.0, 0.2)
            await asyncio.sleep(delay + jitter)
            attempt += 1
