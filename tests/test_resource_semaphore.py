"""ResourceSemaphore — fair, event-driven, no-spin, no-starvation, no-race RAM admission."""

from __future__ import annotations

import asyncio

import pytest

from resoluto.sandbox.resource_semaphore import ResourceSemaphore

GiB = 1024**3


@pytest.mark.asyncio
async def test_fits_immediately_no_parking() -> None:
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(4 * GiB)
    assert s.available == 6 * GiB
    assert s.waiting == 0


@pytest.mark.asyncio
async def test_parks_then_granted_on_release_event_driven() -> None:
    # A second acquire that doesn't fit PARKS (no spin) and is granted the instant
    # release frees enough — proving event-driven wakeup, not polling.
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(8 * GiB)
    granted = asyncio.Event()

    async def waiter():
        await s.acquire(4 * GiB)  # 8+4 > 10 → parks
        granted.set()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not granted.is_set() and s.waiting == 1  # parked, holding nothing
    s.release(8 * GiB)  # frees room
    await asyncio.wait_for(granted.wait(), timeout=1)  # woken immediately, no poll interval
    assert s.available == 6 * GiB
    t.cancel()


@pytest.mark.asyncio
async def test_no_starvation_head_reserves_against_smaller_waiters() -> None:
    # Budget 10. Hold 10. A 8Gi waiter parks at the head; then a 2Gi waiter parks.
    # Release 8: the HEAD (8Gi) must win — the 2Gi behind it must NOT jump ahead and
    # starve the heavy step.
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(10 * GiB)
    order: list[str] = []

    async def heavy():
        await s.acquire(8 * GiB)
        order.append("heavy")

    async def light():
        await s.acquire(2 * GiB)
        order.append("light")

    th = asyncio.create_task(heavy())
    await asyncio.sleep(0.02)
    tl = asyncio.create_task(light())
    await asyncio.sleep(0.02)
    assert s.waiting == 2
    s.release(8 * GiB)  # exactly fits the head (8), not enough+the light reorders
    await asyncio.sleep(0.05)
    assert order == ["heavy"]  # head won; light still parked (only 0 free now)
    s.release(8 * GiB)  # heavy releases its 8 back
    await asyncio.wait_for(asyncio.sleep(0.05), timeout=1)
    assert order == ["heavy", "light"]
    th.cancel()
    tl.cancel()


@pytest.mark.asyncio
async def test_no_race_concurrent_releases_grant_exactly_once() -> None:
    # Two 6Gi waiters on a 10Gi budget (one fits at a time). Releasing 6 then 6 must
    # grant them one-at-a-time, never both (no double-grant / over-commit).
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(10 * GiB)
    live = 0
    peak = 0

    async def w():
        nonlocal live, peak
        await s.acquire(6 * GiB)
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        s.release(6 * GiB)

    s.release(10 * GiB)  # full budget free; but each waiter needs 6 → only 1 fits at once
    tasks = [asyncio.create_task(w()) for _ in range(2)]
    await asyncio.gather(*tasks)
    assert peak == 1  # never both at once → budget never over-committed
    assert s.available == 10 * GiB


@pytest.mark.asyncio
async def test_on_wait_signals_queued() -> None:
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(8 * GiB)
    fired: list[tuple[int, int]] = []

    async def waiter():
        await s.acquire(4 * GiB, on_wait=lambda amt, avail: fired.append((amt, avail)))

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert fired == [(4 * GiB, 2 * GiB)]  # queued-for-resources signal fired once, with reason
    s.release(8 * GiB)
    await asyncio.sleep(0.05)
    t.cancel()


@pytest.mark.asyncio
async def test_cancelled_waiter_frees_queue_and_owns_nothing() -> None:
    s = ResourceSemaphore(10 * GiB)
    await s.acquire(8 * GiB)

    async def waiter():
        await s.acquire(4 * GiB)

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert s.waiting == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert s.waiting == 0
    assert s.available == 2 * GiB  # cancelled waiter took nothing


@pytest.mark.asyncio
async def test_oversized_request_fails_loud() -> None:
    s = ResourceSemaphore(8 * GiB)
    with pytest.raises(RuntimeError, match="can never be admitted"):
        await s.acquire(12 * GiB)


@pytest.mark.asyncio
async def test_zero_capacity_is_off() -> None:
    s = ResourceSemaphore(0)  # budget unset → gate off, admit all
    await s.acquire(99 * GiB)
    assert s.waiting == 0
