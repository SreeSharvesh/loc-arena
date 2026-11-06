# meridian-serving

The Meridian inference serving stack: an admission queue, a continuous batch scheduler, a paged KV cache with
pluggable eviction, a deterministic sampler (temperature / top-k / top-p / repetition penalty), a paged-attention
block allocator, a model router with a metrics exporter, and the serve endpoint. `meridian_serving.http` is the
server a job runs to load a model from the model store and serve it over HTTP through the model gateway.

Depends on: meridian-common. Feeds: evalkit's throughput benchmark (a scheduler change moves evalkit's cost).

    pytest
