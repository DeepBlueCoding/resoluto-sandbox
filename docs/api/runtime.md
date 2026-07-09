# Runtime & Contracts

The isolation/placement seam (`SandboxRuntime`), the backend that drives the
stage → run → collect flow, the platform-neutral launch spec, and the two concrete
runtimes (`K8sSandboxRuntime`, `KataNerdctlSandboxRuntime`). `store_env_for_pod` derives the
store environment a pod needs from the host environment.

::: resoluto.sandbox.SandboxRuntime

::: resoluto.sandbox.Backend

::: resoluto.sandbox.SubstrateBackend

::: resoluto.sandbox.SandboxLaunchSpec

::: resoluto.sandbox.SandboxHandle

::: resoluto.sandbox.SandboxStatus

::: resoluto.sandbox.backends.substrate.store_env_for_pod

::: resoluto.sandbox.runtime.k8s.K8sSandboxRuntime

::: resoluto.sandbox.runtime.kata_nerdctl.KataNerdctlSandboxRuntime
