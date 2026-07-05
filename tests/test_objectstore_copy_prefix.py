"""copy_prefix — carries a run's lane substrate forward on resume (one worker-owned
resume covering the stepped lanes). Verified on LocalConduit (the default + the dev backend)."""
from resoluto.sandbox.conduit import LocalConduit


async def _seed(store):
    # mimic a lane substrate under run/A/nodes/compete/lane-0
    await store.put("run/A/nodes/compete/lane-0/checkpoint.json", b'{"next_step":"gate:project_gate"}')
    await store.put("run/A/nodes/compete/lane-0/worktree/inbox/workspace.tar.gz", b"TARBYTES")
    await store.put("run/A/nodes/compete/lane-0/steps/a0-agent/result.json", b'{"ok":true}')
    await store.put("run/A/nodes/compete/lane-0/lane_job.json", b'{"run_id":"A"}')


async def test_copy_prefix_mirrors_suffixes_and_bytes(tmp_path):
    store = LocalConduit(tmp_path)
    await _seed(store)

    n = await store.copy_prefix("run/A/nodes", "run/B/nodes")

    assert n == 4
    src = {o.key[len("run/A/nodes"):]: o.size for o in await store.list_prefix("run/A/nodes")}
    dst = {o.key[len("run/B/nodes"):]: o.size for o in await store.list_prefix("run/B/nodes")}
    assert src == dst  # suffix-for-suffix, same sizes
    # bytes are identical
    assert await store.get("run/B/nodes/compete/lane-0/worktree/inbox/workspace.tar.gz") == b"TARBYTES"
    assert await store.get("run/B/nodes/compete/lane-0/checkpoint.json") == b'{"next_step":"gate:project_gate"}'


async def test_copy_prefix_absent_source_is_noop(tmp_path):
    store = LocalConduit(tmp_path)
    assert await store.copy_prefix("run/NOPE/nodes", "run/C/nodes") == 0
    assert await store.list_prefix("run/C/nodes") == []  # nothing created


async def test_copy_prefix_is_idempotent(tmp_path):
    store = LocalConduit(tmp_path)
    await _seed(store)
    await store.copy_prefix("run/A/nodes", "run/B/nodes")
    n2 = await store.copy_prefix("run/A/nodes", "run/B/nodes")  # second run overwrites
    assert n2 == 4
    assert len(await store.list_prefix("run/B/nodes")) == 4  # no duplication
