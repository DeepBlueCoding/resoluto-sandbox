# Pool

Bounded concurrency over sandbox slots: acquire a lease, run inside it, release it back.
`Admission` is the concurrency-admission decision and `Lease` the granted slot the pool hands out.

::: resoluto.sandbox.SandboxPool

::: resoluto.sandbox.SandboxLease

::: resoluto.sandbox.Admission

::: resoluto.sandbox.Lease
