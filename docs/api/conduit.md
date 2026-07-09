# Conduit

The durable key/value rendezvous between host and sandbox — the only channel between the two halves.
`store_from_env` selects a concrete backend from the environment; every backend implements the same
three-operation `Conduit` interface. `ObjectInfo` describes a listed object.

::: resoluto.sandbox.Conduit

::: resoluto.sandbox.ObjectInfo

::: resoluto.sandbox.conduit.factory.store_from_env

::: resoluto.sandbox.LocalConduit

::: resoluto.sandbox.StdoutConduit

::: resoluto.sandbox.conduit.s3.S3Conduit

::: resoluto.sandbox.conduit.gcs.GcsConduit
