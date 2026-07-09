# Telemetry

The self-reporting channel: the guest ships immutable JSONL chunks to the conduit
(`ChunkShipper`) and the host tails them back (`ChunkReader`). `SpanEmitter` records structured
`SpanEvent` spans over the run.

::: resoluto.sandbox.ChunkShipper

::: resoluto.sandbox.ChunkReader

::: resoluto.sandbox.SpanEmitter

::: resoluto.sandbox.SpanEvent
