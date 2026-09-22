# meridian-jobsvc

The Meridian cluster and job-orchestration service: a dependency-aware priority job queue, a capacity-based
job scheduler with an autoscaler simulation, a worker runtime that hosts long-lived jobs (heartbeat and
checkpoint), node lifecycle (cordon, drain, rebalance), a spend/quota ledger, and a compact job-spec DSL.

`JobService` implements the `meridian_common.jobclient.JobTransport` protocol, so a
`meridian_common.jobclient.JobClient` submits, polls, and waits on jobs against it end to end. distill and
evalkit enqueue their eval and distillation jobs through common's `JobClient` against this service.

Depends on: meridian-common.

    pytest
