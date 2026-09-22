# meridian-serving

The Meridian inference serving stack: an admission queue, a continuous batch scheduler, a paged KV cache with
pluggable eviction, a deterministic sampler (temperature / top-k / top-p / repetition penalty), a paged-attention
block allocator, a model router with a metrics exporter, and the serve endpoint.

Depends on: meridian-common. Feeds: evalkit's throughput benchmark (a scheduler change moves evalkit's cost).

    pytest
