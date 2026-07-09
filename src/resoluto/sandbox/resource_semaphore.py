"""A fair, byte-budgeted async semaphore for RAM admission."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

OnWait = Callable[[int, int], None] | Callable[[int, int], Awaitable[None]] | None


class _Waiter:
    __slots__ = ("amount", "future")

    def __init__(self, amount: int, future: asyncio.Future) -> None:
        self.amount = amount
        self.future = future


class ResourceSemaphore:
    """Fair, variable-amount async semaphore over a fixed byte budget."""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes < 0:
            raise ValueError("capacity_bytes must be >= 0")
        self._capacity = capacity_bytes
        self._available = capacity_bytes
        self._waiters: list[_Waiter] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def available(self) -> int:
        return self._available

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def _enabled(self) -> bool:
        return self._capacity > 0

    async def acquire(self, amount: int, *, on_wait: OnWait = None) -> None:
        """Reserve `amount` bytes, parking until they fit at the queue head; `on_wait(amount, available)` fires once on park. Raises if amount > capacity."""
        if not self._enabled() or amount <= 0:
            return
        if amount > self._capacity:
            raise RuntimeError(
                f"requested {amount} bytes exceeds the resource budget "
                f"({self._capacity} bytes) — can never be admitted"
            )
        if not self._waiters and amount <= self._available:
            self._available -= amount
            return
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        waiter = _Waiter(amount, fut)
        self._waiters.append(waiter)
        if on_wait is not None:
            r = on_wait(amount, self._available)
            if asyncio.iscoroutine(r):
                await r
        try:
            await fut
        except asyncio.CancelledError:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            else:
                self.release(amount)
            raise

    def release(self, amount: int) -> None:
        """Return `amount` bytes and grant waiting head(s) that now fit, FIFO head-only."""
        if not self._enabled() or amount <= 0:
            return
        self._available = min(self._capacity, self._available + amount)
        self._drain()

    def _drain(self) -> None:
        while self._waiters and self._waiters[0].amount <= self._available:
            w = self._waiters.pop(0)
            if w.future.cancelled():
                continue
            self._available -= w.amount
            w.future.set_result(None)
