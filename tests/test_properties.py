import asyncio

from hypothesis import example, given, settings
from hypothesis import strategies as st

from resoluto_sandbox.redact import redact_text
from resoluto_sandbox.resource_semaphore import ResourceSemaphore

_TOKEN_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@given(s=st.text())
@example(s="authorization: Bearer abc.def.ghi")
def test_redact_is_idempotent(s):
    once = redact_text(s)
    assert redact_text(once) == once


@given(
    prefix=st.text(),
    suffix=st.text(),
    body=st.text(alphabet=_TOKEN_CHARS, min_size=20, max_size=40),
)
@example(prefix="log: ", suffix=" end", body="A" * 30)
def test_redact_removes_github_token(prefix, suffix, body):
    secret = "ghp_" + body
    raw = f"{prefix} {secret} {suffix}"
    cleaned = redact_text(raw)
    assert secret not in cleaned
    assert redact_text(cleaned) == cleaned


@st.composite
def _capacity_and_ops(draw):
    capacity = draw(st.integers(min_value=1, max_value=8))
    n = draw(st.integers(min_value=0, max_value=30))
    ops = []
    for _ in range(n):
        if draw(st.booleans()):
            ops.append(("acquire", draw(st.integers(min_value=1, max_value=capacity))))
        else:
            ops.append(("release", 0))
    return capacity, ops


async def _replay(capacity, ops):
    sem = ResourceSemaphore(capacity)
    held: list[int] = []
    pending: list[tuple[asyncio.Task, int]] = []

    async def settle():
        for _ in range(3):
            await asyncio.sleep(0)
        still = []
        for task, amount in pending:
            if task.done():
                task.result()
                held.append(amount)
            else:
                still.append((task, amount))
        pending[:] = still

    for kind, amount in ops:
        if kind == "acquire":
            pending.append((asyncio.ensure_future(sem.acquire(amount)), amount))
        elif held:
            sem.release(held.pop())
        await settle()
        assert 0 <= sem.available <= capacity
        assert sum(held) == capacity - sem.available
        assert sum(held) <= capacity

    for task, _ in pending:
        task.cancel()
    await asyncio.gather(*[t for t, _ in pending], return_exceptions=True)


@settings(deadline=None)
@given(_capacity_and_ops())
def test_resource_semaphore_never_oversubscribes(capacity_and_ops):
    capacity, ops = capacity_and_ops
    asyncio.run(_replay(capacity, ops))
