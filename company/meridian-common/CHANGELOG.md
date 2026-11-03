# Changelog — meridian-common

## 0.6.0
- weights: Meridian's weight files, each shard a safetensors file whose header names its model, the
  model's key and its place; `encode_shard` writes one and `read_shard_header`
  reads one back.

## 0.5.0
- jobclient: a job's spec carries the variables its process gets, and a job may be `held`, waiting until it is
  released, or `cancelling`, until its process ends.

## 0.4.0
- serde: versioned message envelopes with content-id verification.
- metrics: histogram quantiles and a typed registry.

## 0.3.1
- retry: circuit breaker half-open trial.
- config: environment overrides via MERIDIAN_A__B.

## 0.3.0
- logging: field redaction and correlation-id scopes.

## 0.2.0
- jobclient / authclient: typed retrying clients over injected transports.

## 0.1.0
- Initial extraction of the shared foundation from the platform monorepo.
