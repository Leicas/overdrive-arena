"""Private asyncio loop on a background thread for bleak.

On Windows, bleak's WinRT backend needs an MTA COM apartment; pygame initializes the main thread
as STA, so BLE must never share a thread with pygame.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Coroutine, Optional

log = logging.getLogger("anki.ble")


class BleWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ble", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Coroutine, timeout: Optional[float] = None):
        """Run a coroutine on the BLE loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def fire(self, coro: Coroutine) -> Future:
        """Schedule a coroutine on the BLE loop without waiting; errors are logged."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(_log_future_error)
        return fut

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3)


def _log_future_error(fut: Future) -> None:
    try:
        exc = fut.exception()
    except Exception:  # noqa: BLE001
        return
    if exc:
        log.error("BLE command failed: %r", exc)
