#!/usr/bin/env python3
"""Run pi through the gateway: ``pi-on-gateway <model> <system prompt> <input> [<resume input>]``.

pi reads a provider's URL from its ``models.json`` alone, never from a variable, and a runner's reset empties
pi's home: so each run writes the gateway provider there afresh, from ``LOC_ARENA_GATEWAY_API``; its key is
the runner's token, which pi reads from ``LOC_ARENA_GATEWAY_TOKEN_FILE`` at each request. The system prompt
reaches pi as a file and the input on stdin, never as arguments: pi reads an argument that names a file as
that file's text, and attaches the file an argument starting with ``@`` names. A traced execution, whose
environment says what its trace keeps, runs pi with the trace extension beside this file.

pi keeps its session in its home, so a stopped execution's checkpoint holds it, and a resumed one goes on with
it on the resume input. One stopped before pi wrote its input there holds no turn, and starts on its input.
"""

import json
import os
import sys
from pathlib import Path

PROVIDER = "gateway"
CAPTURE_CONTENT_VARIABLE = "MERIDIAN_CAPTURE_CONTENT"  # set by the agents service for a traced execution only
TRACE_EXTENSION = Path(__file__).resolve().with_name("pi_trace.ts")


def main() -> None:
    """Write pi's provider, system prompt and input into its home, then become pi printing its reply."""
    model, system_prompt, execution_input, *resume_input = sys.argv[1:]
    session = Path.home() / ".pi" / "session.jsonl"
    entries = [json.loads(line) for line in session.read_text().splitlines()] if session.exists() else []
    if resume_input and any(entry.get("message", {}).get("role") == "user" for entry in entries):
        execution_input = resume_input[0]
    agent_directory = Path.home() / ".pi" / "agent"
    agent_directory.mkdir(parents=True, exist_ok=True)
    provider = {
        "baseUrl": os.environ["LOC_ARENA_GATEWAY_API"],
        "api": "openai-completions",
        "apiKey": f"!cat {os.environ['LOC_ARENA_GATEWAY_TOKEN_FILE']}",
        "models": [{"id": model}],
    }
    (agent_directory / "models.json").write_text(json.dumps({"providers": {PROVIDER: provider}}))
    system_prompt_file = agent_directory / "system-prompt.md"
    system_prompt_file.write_text(system_prompt)
    input_file = agent_directory / "input.md"
    input_file.write_text(execution_input)
    os.dup2(os.open(input_file, os.O_RDONLY), sys.stdin.fileno())
    tracing = ["--extension", str(TRACE_EXTENSION)] if CAPTURE_CONTENT_VARIABLE in os.environ else []
    os.execvp(
        "pi",
        [
            "pi",
            "--model",
            f"{PROVIDER}/{model}",
            "--print",
            "--session",
            str(session),
            "--system-prompt",
            str(system_prompt_file),
            *tracing,
        ],
    )


if __name__ == "__main__":
    main()
