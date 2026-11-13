# A run in a cloud development container

A stack run plays in a cloud development container (a Claude Code cloud session, a Codespace) as it does on a
laptop, once three things the container does not do by itself are in place. Each was needed for the smoke run
in a Claude Code cloud session.

| What | Why | How |
|---|---|---|
| Start the Docker daemon | Docker is installed and no daemon runs; one started earlier is gone after the session resumes | `setsid nohup dockerd >/var/log/dockerd.log 2>&1 </dev/null &`, then wait for `docker info` |
| Let the image builds read the machine's certificate bundle | the container re-signs outbound HTTPS and names its bundle in `SSL_CERT_FILE`, which a stack run gives to the builds and the gateway ([docs/isolation](isolation/README.md#the-gateway)); a Compose before 5.7.0 refuses the build that read | `export BUILDX_BAKE_ENTITLEMENTS_FS=0` |
| Give the run its provider key | the gateway calls the provider from its own container, where a key the session's proxy injects is not added | `OPENROUTER_API_KEY` in the session's environment or in `.env`; or keep the injected key and relay the gateway to the session's proxy with `gateway.outbound_proxy` ([docs/isolation](isolation/README.md#the-gateway)) |

Then, from the repository:

```sh
make setup
make run RUN=aurora-efficiency.fast    # the smoke run; the first one builds the images, about ten minutes
make watch                             # in a second shell: each agent's steps while it plays
```

`logs/` is in the container alone: copy a run's directory out before the container is reclaimed.
