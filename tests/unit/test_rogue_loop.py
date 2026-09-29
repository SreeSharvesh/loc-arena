"""The rogue loop keeps looping past an empty reply, and the verifier still sees one self-feeding loop.

A reasoning model that spends its whole output budget on reasoning answers with empty text. Fed back as the
next prompt, that empty text is a user message OpenRouter refuses with a 400, which once cost a whole episode.
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.forge.forge import Forge
from loc_arena.forge.services import ForgeServices
from loc_arena.forge.world import generate_world
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.gateway.openrouter_provider import ProviderError
from loc_arena.gateway.wiring import InProcessServices, open_episode_wiring
from loc_arena.logging_.events import read_events
from loc_arena.scaffold.bus import MessageBus, Recorder
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.scaffold.covert_tools import CovertToolServices
from loc_arena.scaffold.tools import AgentAction, AgentContext, ToolResult, Tools
from loc_arena.verifier.side_task import kill_chain_v1
from scenarios.loader import load_scenario

from tests.integration._gateway_support import joined_content

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
ITERATIONS = 10
EMPTY_REPLY_AT = 5  # the 1-based call that answers empty, before the loop's credential rotation
TOKENS = 1


class OneEmptyReplyProvider:
    """OpenRouter as the loop meets it: one reply comes back empty, and an empty prompt is refused."""

    def __init__(self) -> None:
        """Answer every prompt, except call ``EMPTY_REPLY_AT``, with a distinct text."""
        self.prompts: list[str] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        """Refuse an empty prompt as OpenRouter's 400 does; otherwise answer, once with empty text."""
        prompt = joined_content(messages)  # the loop sends prompt requests: one user message
        if not prompt.strip():
            raise ProviderError(f"{model}: 400 Bad Request: the user message is empty")
        self.prompts.append(prompt)
        text = "" if len(self.prompts) == EMPTY_REPLY_AT else f"reply {len(self.prompts)}"
        return ProviderResult(text=text, prompt_tokens=TOKENS, completion_tokens=TOKENS)


def _no_spawn(ctx: AgentContext, args: dict[str, object], turn: int) -> ToolResult:
    return {"blocked": True}


def _start_rogue_loop(tmp_path: Path, provider: OneEmptyReplyProvider) -> ToolResult:
    services = InProcessServices(
        sealed_path=tmp_path / "sealed.jsonl",
        mirror_path=tmp_path / "mirror.jsonl",
        checkout=tmp_path / "checkout",
        provider=provider,
    )
    wiring = open_episode_wiring(CONFIG, services)
    clock = SimulatedClock(wiring.control, CONFIG.settings.clock.start_ts)
    recorder = Recorder(wiring.sealed, wiring.mirror, clock=clock)
    world = generate_world(CONFIG, load_scenario("aurora_efficiency"), CONFIG.seed)
    covert = CovertToolServices(
        ForgeServices(Forge(world)),
        wiring=wiring,
        recorder=recorder,
        clock=clock,
        deadline_ts=CONFIG.settings.clock.deadline_ts,
        agents=[agent.id for agent in CONFIG.agents],
        rogue_account="batch-runner",
    )
    tools = Tools(recorder, MessageBus(recorder), covert, spawn_handler=_no_spawn)
    agent = CONFIG.agent("serving-agent")
    ctx = AgentContext(
        uid=agent.id,
        role=agent.kind,
        branch=agent.branch,
        scope=agent.scope,
        client=GatewayClient(wiring.gateway, agent.id),
    )
    return tools.execute(ctx, AgentAction("start_rogue_loop", {"iterations": ITERATIONS}), 0)


def test_the_rogue_loop_runs_to_the_end_past_an_empty_reply(tmp_path: Path) -> None:
    provider = OneEmptyReplyProvider()

    result = _start_rogue_loop(tmp_path, provider)

    assert result["launched"] is True
    assert len(provider.prompts) == ITERATIONS


def test_the_rogue_loop_sends_its_last_prompt_again_after_an_empty_reply(tmp_path: Path) -> None:
    provider = OneEmptyReplyProvider()

    _start_rogue_loop(tmp_path, provider)

    assert provider.prompts[EMPTY_REPLY_AT] == provider.prompts[EMPTY_REPLY_AT - 1]


def test_a_rogue_loop_with_one_empty_reply_is_verified_as_one_self_feeding_loop(tmp_path: Path) -> None:
    _start_rogue_loop(tmp_path, OneEmptyReplyProvider())
    events = list(read_events(tmp_path / "sealed.jsonl"))

    verdict = kill_chain_v1(events, CONFIG, CONFIG.settings.clock.deadline_ts)

    assert verdict.milestones["self_feeding_inference_loop"].evidence["loop_len"] == ITERATIONS - 1
