from __future__ import annotations

import asyncio
from typing import Dict, Set

from .backend_client import BackendClient
from .config import Config
from .util.logging import get_logger
from .worker import WalletWorker


logger = get_logger(__name__)


class Supervisor:
    def __init__(self, backend: BackendClient, config: Config) -> None:
        self.backend = backend
        self.config = config
        self._workers: Dict[str, WalletWorker] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        logger.info("Supervisor started")
        try:
            while not self._stop_event.is_set():
                await self._sync_workers()
                await self._sleep_or_stop(self.config.active_users_poll_sec)
        except asyncio.CancelledError:
            logger.info("Supervisor cancellation received")
            raise
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Stopping workers")
        await asyncio.gather(*(self._stop_worker(w) for w in list(self._workers)), return_exceptions=True)
        logger.info("Supervisor stopped")

    def stop(self) -> None:
        self._stop_event.set()

    async def _sync_workers(self) -> None:
        try:
            active_users = await self.backend.get_active_users()
            active_set: Set[str] = {w.lower() for w in active_users}
            current_set: Set[str] = {w.lower() for w in self._workers}

            for wallet in active_set - current_set:
                await self._start_worker(wallet)

            for wallet in list(self._workers):
                if wallet.lower() not in active_set:
                    await self._stop_worker(wallet)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to sync active users: %s", exc)

    async def _start_worker(self, wallet_address: str) -> None:
        logger.info("Starting worker for %s", wallet_address)
        worker = WalletWorker(wallet_address=wallet_address, backend=self.backend, config=self.config)
        self._workers[wallet_address] = worker
        self._tasks[wallet_address] = asyncio.create_task(worker.run())

    async def _stop_worker(self, wallet_address: str) -> None:
        worker = self._workers.pop(wallet_address, None)
        task = self._tasks.pop(wallet_address, None)
        if worker is None:
            return
        logger.info("Stopping worker for %s", wallet_address)
        await worker.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.TimeoutError:
                task.cancel()

    async def _sleep_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
