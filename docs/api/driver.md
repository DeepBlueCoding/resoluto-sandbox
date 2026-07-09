# Driver

Drive one node through a sandbox end to end: stage its inputs, run it, collect its outputs, and map
the raw exit into a typed outcome. `drive_node` is the high-level entry; `drive_node_raw` exposes the
un-mapped result; `run_node_in_sandbox` is the low-level single-shot runner.

::: resoluto.sandbox.drive_node

::: resoluto.sandbox.drive_node_raw

::: resoluto.sandbox.NodeOutcome

::: resoluto.sandbox.NodeResult

::: resoluto.sandbox.run_node_in_sandbox
