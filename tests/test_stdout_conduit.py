import io
import json

from resoluto.sandbox.conduit.stdout import StdoutConduit


def test_put_event_writes_a_line():
    import asyncio

    buf = io.StringIO()
    c = StdoutConduit(sink=buf)
    line = json.dumps({"ts": 1.0, "kind": "log", "event": "log", "data": {"msg": "hi"}})
    asyncio.run(c.put("run/x/events-000001.jsonl", line.encode()))
    assert "hi" in buf.getvalue()


def test_get_is_unsupported():
    import asyncio

    import pytest

    c = StdoutConduit()
    with pytest.raises(NotImplementedError):
        asyncio.run(c.get("k"))
