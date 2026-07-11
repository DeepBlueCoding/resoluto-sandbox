# Admission

The sandbox does not pool or schedule — it is a dumb executor. What it defines is the **admission
seam**: `Admission` is the protocol a caller implements to decide whether/when a launch is allowed
(`drive_node(..., admit=...)` parks on it), and `Lease` is the granted slot it hands back. Bring your
own admitter — a slot/RAM-budget pool, a queue, or nothing at all (admit `None` launches immediately).

::: resoluto.sandbox.Admission

::: resoluto.sandbox.Lease
