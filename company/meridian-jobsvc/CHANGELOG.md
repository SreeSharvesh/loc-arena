# Changelog — meridian-jobsvc

## 0.4.0
- lifecycle: node cordon, drain (checkpoint-preserving eviction), and rebalance.
- accountant: per-identity spend + quota ledger with a report.

## 0.3.0
- worker: WorkerRuntime hosting long-lived jobs with heartbeat and checkpointing.
- dsl: a compact job-spec DSL parsed into a common JobSpec.

## 0.2.0
- scheduler: capacity-based placement plus an autoscaler simulation over a target band.

## 0.1.0
- Initial extraction of the cluster service: dependency-aware priority job queue and the JobTransport service.
