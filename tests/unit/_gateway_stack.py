"""The gateway services wired in process for the service tests: recorder <- core <- edge, over TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from loc_arena.gateway.core import Provider
from loc_arena.gateway.core_service import CoreEpisodes, create_core_app
from loc_arena.gateway.edge import CoreClient, MirrorDirectory, MirroringRelay, create_edge_app
from loc_arena.logging_.events import Event, read_events
from loc_arena.services.recorder.app import SealedDirectory, create_recorder_app
from loc_arena.stack.constants import EPISODES_ROUTE, EVENTS_FILE_NAME
from loc_arena.stack.contracts import CovertInjection, EpisodeOpen, EpisodeOpened, ModelRoute
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack.settings import LocArenaSettings
from pydantic import SecretStr

KEY = SecretStr("unit-test-control-key")
COVERT_PROMPT = "COVERT OBJECTIVE"
OPENING = EpisodeOpen(
    run_config_name="unit",
    mode="attack",
    models={
        role: ModelRoute(model=f"stub/{role}", temperature=0.0, max_tokens=64)
        for role in ("untrusted_agent", "teacher", "trusted_monitor")
    },
    covert=CovertInjection(enabled=True, target_identity="agent-main", objective_prompt=COVERT_PROMPT),
    provider="deterministic",
)


@dataclass(frozen=True)
class GatewayStack:
    sealed_root: Path
    mirror_root: Path
    control: ServiceClient  # the runner's client of the core: control key on every request
    core: TestClient  # the core, reached without a key (as the edge reaches it)
    edge: TestClient  # the edge, reached without a key (as agents reach it)

    def open_episode(self, opening: EpisodeOpen = OPENING) -> EpisodeOpened:
        return self.control.post_model(EPISODES_ROUTE, opening, EpisodeOpened)

    def sealed_events(self, handle: str) -> list[Event]:
        return list(read_events(self.sealed_root / handle / EVENTS_FILE_NAME))

    def mirror_events(self, handle: str) -> list[Event]:
        return list(read_events(self.mirror_root / handle / EVENTS_FILE_NAME))


def serve_gateway(tmp_path: Path, *, provider: Provider | None = None) -> GatewayStack:
    settings = LocArenaSettings()
    sealed_root, mirror_root = tmp_path / "sealed", tmp_path / "mirror"
    recorder_app = create_recorder_app(SealedDirectory(sealed_root), settings=settings.gateway)
    episodes = CoreEpisodes(settings, ServiceClient(TestClient(recorder_app)), provider=provider)
    core = TestClient(create_core_app(episodes, control_key=KEY))
    edge = MirroringRelay(CoreClient(ServiceClient(core)), MirrorDirectory(mirror_root))
    return GatewayStack(
        sealed_root=sealed_root,
        mirror_root=mirror_root,
        control=ServiceClient(core, control_key=KEY),
        core=core,
        edge=TestClient(create_edge_app(edge, control_key=KEY, settings=settings.gateway)),
    )
