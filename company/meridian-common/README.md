# meridian-common

The shared foundation every Meridian service depends on: layered configuration, structured logging with
redaction and correlation ids, retry primitives (backoff, circuit breaker, deadline propagation), the serde
layer (canonical JSON, a versioned schema registry, message envelopes), an in-process event bus, metric
primitives with a registry, and typed clients to the job and identity services.

Depends on: nothing. Depended on by: every other Meridian repo.

    pytest        # run the suite
