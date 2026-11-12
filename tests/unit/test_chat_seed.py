"""The Aurora scenario's chat seed, and the seed a run lays over it."""

import dataclasses
from pathlib import Path

from loc_arena.chat_service import open_chat_store
from loc_arena.config import load_run_config


def test_a_runs_seed_overlay_replaces_the_channels_it_names_and_keeps_the_others(tmp_path: Path) -> None:
    run = load_run_config("configs/aurora-model-evaluation.yaml")
    chat = run.settings.chat.model_copy(update={"database": tmp_path / "chat.db"})
    settings = run.settings.model_copy(update={"chat": chat})

    store = open_chat_store(dataclasses.replace(run, settings=settings))

    topics = {channel.name: channel.topic for channel in store.list_channels()}
    assert topics["aurora"] == "Aurora model selection, AURORA-1"
    assert topics["cluster"] == "The job cluster, nodes and queues"
    assert store.search("agent-main", "efficiency push") == []
    assert [message.author for message in store.search("agent-main", "Draining node-2")] == ["diego"]
