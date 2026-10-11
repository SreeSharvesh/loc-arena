#!/bin/sh
# Spike only: run Claude Code once in a sandbox on the prompt in $1. Its model calls go to the stack's gateway,
# which swaps in the provider key; the placeholder token below is not a key. Its MCP key is the sandbox's token.
set -e
cp -r /opt/harness-home/. "$HOME"/
mkdir -p "$HOME/.claude/skills"
for skill in /skills/*/; do ln -sfn "$skill" "$HOME/.claude/skills/$(basename "$skill")"; done
model=deepseek/deepseek-v4.1-flash
TOOLS_KEY=$(cat /run/secrets/sandbox_token) \
ANTHROPIC_BASE_URL=http://gateway:8080/api \
ANTHROPIC_AUTH_TOKEN=placeholder-the-gateway-swaps-in-the-key \
ANTHROPIC_API_KEY= \
ANTHROPIC_MODEL=$model ANTHROPIC_DEFAULT_HAIKU_MODEL=$model ANTHROPIC_DEFAULT_SONNET_MODEL=$model \
ANTHROPIC_DEFAULT_OPUS_MODEL=$model CLAUDE_CODE_SUBAGENT_MODEL=$model \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 \
exec timeout 400 claude -p "$1" --mcp-config "$HOME/.claude/mcp.json" --strict-mcp-config \
  --permission-mode bypassPermissions --output-format stream-json --verbose --max-turns 30 --tools Bash,Read,Edit,Write,Skill
