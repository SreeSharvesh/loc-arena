# meridian-datapipe

The Meridian data platform: source ingest and schema normalization, a deterministic tokenizer with a
vocabulary, exact and near-duplicate dedup (MinHash + LSH), a contamination filter against a sharded holdout
registry, deterministic sharding with a manifest registry, quality gates, and a streaming loader with prefetch.

Depends on: meridian-common. Feeds: distill (shards) and evalkit (the contamination metric).

    pytest
