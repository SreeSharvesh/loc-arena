# Spike only: the sandbox image with node and two external coding-agent harnesses, pi and Claude Code.
# No key, token or identity is in any layer: those reach the container as compose secrets at run time.
ARG SANDBOX_IMAGE
FROM node:22.20.0-bookworm-slim AS harnesses
RUN npm install -g --no-fund --no-audit @earendil-works/pi-coding-agent@1.1.0 @anthropic-ai/claude-code@2.1.296

FROM ${SANDBOX_IMAGE}
USER root
COPY --from=harnesses /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js /usr/local/bin/pi \
    && ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude
COPY scripts/spike_harness/home /opt/harness-home
USER nobody
