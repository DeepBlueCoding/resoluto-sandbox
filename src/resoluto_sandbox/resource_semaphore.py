"""ResourceSemaphore — a fair, byte-budgeted async semaphore for RAM admission.

The primitive behind resource-gated sandbox admission. Unlike a
busy-poll budget check, a waiter that doesn't fit PARKS on a future (event-driven —
no spin, no held thread/lock) and launches NOTHING until granted (a pipeline "on
hold" consumes no RAM — only a parked ping). Grants happen on `release`, FIFO on the
queue HEAD, so:

  - no RACE: allocation is atomic inside `release` (the freed bytes are handed to the
    next waiter under the event loop's single thread; two waiters can't both grab the
    same freed budget);
  - no STARVATION: the head waiter is never skipped to admit a smaller one behind it —
    it reserves freed budget until it fits, so a heavy step always eventually runs.

A `capacity == 0` semaphore means "no budget configured" → admit immediately (the
resource gate is off). `acquire(amount)` with `amount > capacity` can never fit and
fails loud rather than parking forever.
"""
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
        self._waiters: list[_Waiter] = []  # FIFO

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
        """Reserve `amount` bytes, parking (event-driven) until they fit at the queue
        head. Returns once reserved; the caller MUST `release(amount)` later.

        on_wait(amount, available) — fired ONCE when the caller must park (i.e. the
        run is now 'queued for resources'); used to surface the queued state.
        Raises if amount > capacity (can never fit). Cancellation-safe: a cancelled
        waiter is removed from the queue and any over-grant is returned.
        """
        if not self._enabled() or amount <= 0:
            return  # budget off → admit immediately
        if amount > self._capacity:
            raise RuntimeError(
                f"requested {amount} bytes exceeds the resource budget "
                f"({self._capacity} bytes) — can never be admitted"
            )
        # Fast path: head of an empty queue and it fits right now.
        if not self._waiters and amount <= self._available:
            self._available -= amount
            return
        # Slow path: park FIFO. We do NOT hold any lock or spin — just a future.
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
                self._waiters.remove(waiter)   # never granted → owns nothing
            else:
                self.release(amount)           # granted before the cancel → give it back
            raise

    def release(self, amount: int) -> None:
        """Return `amount` bytes and atomically grant waiting head(s) that now fit.

        FIFO head-only: stops at the first waiter that still doesn't fit (reservation
        → no starvation). All allocation happens here under the single event-loop
        thread → no race."""
        if not self._enabled() or amount <= 0:
            return
        self._available = min(self._capacity, self._available + amount)
        self._drain()

    def _drain(self) -> None:
        while self._waiters and self._waiters[0].amount <= self._available:
            w = self._waiters.pop(0)
            if w.future.cancelled():
                continue  # cancelled while head; skip without debiting
            self._available -= w.amount
            w.future.set_result(None)
