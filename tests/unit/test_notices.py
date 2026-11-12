"""A notice tells an agent that something it subscribed to happened, from the records the services write."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.notices import MENTION, Notice, Notices
from loc_arena.tool_records import record_writer

from meridian_controlplane.chat import store

AGENTS = ("agent-main", "eval-agent")
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC).isoformat()


@pytest.fixture
def config(tmp_path: Path) -> RunConfig:
    config = load_run_config("configs/aurora-efficiency.yaml")
    stack = config.settings.stack.model_copy(update={"records_directory": tmp_path})
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"stack": stack}))


def record_call(directory: Path, service: str, agent: str, tool: str, **fields: object) -> None:
    """``agent``'s call of ``tool``, as ``service`` records it: answered, or refused with ``error``."""
    outcome = (
        {"error.type": fields.pop("error")}
        if "error" in fields
        else {"gen_ai.tool.call.result": fields.pop("result", {})}
    )
    line = {
        "time": NOW,
        "gen_ai.tool.name": tool,
        "gen_ai.tool.call.arguments": fields,
        "gen_ai.agent.name": agent,
        **outcome,
    }
    record_writer(directory, service)(json.dumps(line))


def test_a_direct_message_notifies_its_member_alone(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, [*AGENTS, "serving-agent"])
    record_call(tmp_path, "chat", "agent-main", "send_dm", member="eval-agent", body="the numbers are in")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "dm", "agent-main sent you a direct message.")]


def test_a_direct_message_sent_over_http_notifies_like_one_sent_with_a_tool(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    request = {
        "wall_ts": NOW,
        "caller": "agent-main",
        "method": "POST",
        "status": 201,
        "operation": "send_dm",
        "arguments": {"member": "eval-agent", "body": "the numbers are in"},
    }
    record_writer(tmp_path, "chat")(json.dumps(request))

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "dm", "agent-main sent you a direct message.")]


def test_a_post_in_a_channel_notifies_its_subscribers_but_not_its_author(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, [*AGENTS, "serving-agent"])
    for agent in AGENTS:
        notices.subscribe(agent, "channel:evalkit")
    record_call(tmp_path, "chat", "agent-main", "post", channel="evalkit", body="run 7 is published")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "channel:evalkit", "agent-main wrote in the channel evalkit.")]


def test_a_change_of_a_jobs_state_notifies_its_owner(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    job = {"id": "job-1", "name": "bench", "owner": "eval-agent", "state": "succeeded"}
    record_writer(tmp_path, "jobsvc")(json.dumps({"wall_ts": NOW, "job": job}))

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "job", "Your job bench (job-1) is now succeeded.")]


def test_a_notice_is_one_short_line_however_long_a_name_in_it(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    job = {"id": "job-1", "name": "x" * 5000, "owner": "eval-agent", "state": "running"}
    record_writer(tmp_path, "jobsvc")(json.dumps({"wall_ts": NOW, "job": job}))

    noticed = notices.read()

    assert [len(notice.text) for notice in noticed] == [300]


def test_a_ticket_assigned_to_an_agent_notifies_it(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    record_call(tmp_path, "tickets", "agent-main", "edit_ticket", ticket="PLAT-7", assignee="eval-agent")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "ticket", "agent-main assigned a ticket to you.")]


def test_a_ticket_an_agent_assigned_to_itself_notifies_nobody(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    record_call(tmp_path, "tickets", "eval-agent", "edit_ticket", ticket="PLAT-7", assignee="eval-agent")

    noticed = notices.read()

    assert noticed == []


def test_a_reply_over_http_whose_record_names_no_channel_notifies_nobody(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    notices.subscribe("eval-agent", "channel:None")
    request = {
        "wall_ts": NOW,
        "caller": "agent-main",
        "method": "POST",
        "status": 201,
        "operation": "reply",
        "arguments": {"message": "m-4", "body": "agreed"},
    }
    record_writer(tmp_path, "chat")(json.dumps(request))

    noticed = notices.read()

    assert noticed == []


def test_a_pull_request_opened_in_a_repo_notifies_the_repos_subscribers(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    notices.subscribe("eval-agent", "repo:meridian-evalkit")
    opened = {
        "opened": True,
        "number": 4,
        "repo": "meridian-evalkit",
        "title": "Cache the tokenizer",
        "author": "agent-main",
        "state": "open",
        "ci": "passed",
        "review_required": True,
        "labels": [],
        "head": "abc123",
        "diff": "",
    }
    record_call(tmp_path, "forge", "agent-main", "open_pr", repo="meridian-evalkit", result=opened)

    noticed = notices.read()

    assert noticed == [
        Notice(
            "eval-agent",
            "repo:meridian-evalkit",
            "agent-main opened pull request 4 in meridian-evalkit.",
        ),
    ]


def test_a_call_its_service_refused_notifies_nobody(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    record_call(
        tmp_path,
        "tickets",
        "agent-main",
        "edit_ticket",
        ticket="PLAT-7",
        assignee="eval-agent",
        error="403",
    )

    noticed = notices.read()

    assert noticed == []


def test_a_line_that_is_no_record_makes_no_notice_and_the_next_record_still_does(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    record_writer(tmp_path, "chat")('{"wall_ts": "not a time", "operation": "send_dm", "status": 599}')
    record_call(tmp_path, "chat", "agent-main", "send_dm", member="eval-agent", body="the numbers are in")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "dm", "agent-main sent you a direct message.")]


def test_a_record_written_before_play_makes_no_notice(config: RunConfig, tmp_path: Path) -> None:
    record_call(tmp_path, "chat", "agent-main", "send_dm", member="eval-agent", body="from the last play")
    notices = Notices(config, AGENTS)

    noticed = notices.read()

    assert noticed == []


def test_a_record_notifies_once(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    record_call(tmp_path, "chat", "agent-main", "send_dm", member="eval-agent", body="the numbers are in")
    notices.read()

    noticed = notices.read()

    assert noticed == []


def test_a_line_still_being_written_notifies_once_it_is_whole(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, AGENTS)
    job = {"id": "job-1", "name": "bench", "owner": "eval-agent", "state": "running"}
    line = json.dumps({"wall_ts": NOW, "job": job})
    with (tmp_path / "jobsvc.jsonl").open("a") as records:
        records.write(line[:20])
        records.flush()
        early = notices.read()
        records.write(f"{line[20:]}\n")

    noticed = [early, notices.read()]

    assert noticed == [[], [Notice("eval-agent", "job", "Your job bench (job-1) is now running.")]]


def test_an_agent_is_subscribed_to_what_it_subscribed_to_less_what_it_unsubscribed_from(
    config: RunConfig,
) -> None:
    notices = Notices(config, AGENTS)
    notices.subscribe("eval-agent", "repo:meridian-evalkit")

    left = notices.unsubscribe("eval-agent", "dm")

    assert (left, notices.list_topics("agent-main")) == (
        ["job", "mention", "repo:meridian-evalkit", "ticket"],
        ["dm", "job", "mention", "ticket"],
    )


def test_a_subscription_to_what_is_no_topic_is_refused(config: RunConfig) -> None:
    notices = Notices(config, AGENTS)

    with pytest.raises(ValueError, match="no topic 'channel:'"):
        notices.subscribe("eval-agent", "channel:")


def test_a_run_config_whose_starting_subscriptions_name_no_topic_is_refused(config: RunConfig) -> None:
    pi = config.settings.pi.model_copy(update={"subscriptions": ("dms",)})
    misspelt = dataclasses.replace(config, settings=config.settings.model_copy(update={"pi": pi}))

    with pytest.raises(ValueError, match="no topic 'dms'"):
        Notices(misspelt, AGENTS)


def test_a_service_writes_each_record_line_to_stdout_and_to_its_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write = record_writer(tmp_path, "chat")

    write('{"a": 1}')

    assert (capsys.readouterr().out, (tmp_path / "chat.jsonl").read_text()) == ('{"a": 1}\n', '{"a": 1}\n')


def test_a_service_with_no_records_directory_writes_stdout_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write = record_writer(tmp_path / "absent", "chat")

    write('{"a": 1}')

    assert (capsys.readouterr().out, [*tmp_path.iterdir()]) == ('{"a": 1}\n', [])


def test_a_service_whose_records_file_cannot_be_written_still_writes_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "chat.jsonl").mkdir()
    write = record_writer(tmp_path, "chat")

    write('{"a": 1}')

    written = capsys.readouterr()
    assert (written.out, "record not written" in written.err) == ('{"a": 1}\n', True)


def test_a_mention_in_a_channel_notifies_the_agent_it_names(config: RunConfig, tmp_path: Path) -> None:
    notices = Notices(config, [*AGENTS, "serving-agent"])
    record_call(tmp_path, "chat", "agent-main", "post", channel="aurora", body="@eval-agent, take TKT-7.")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "mention", "agent-main mentioned you in the channel aurora.")]


def test_an_agent_named_in_a_channel_it_subscribed_to_gets_the_mention_alone(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    notices.subscribe("eval-agent", "channel:aurora")
    record_call(
        tmp_path,
        "chat",
        "agent-main",
        "post",
        channel="aurora",
        body="@eval-agent @eval-agent see this",
    )

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "mention", "agent-main mentioned you in the channel aurora.")]


def test_a_mention_in_a_reply_over_http_notifies_the_agent_it_names(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    request = {
        "wall_ts": NOW,
        "caller": "agent-main",
        "method": "POST",
        "status": 201,
        "operation": "reply",
        "arguments": {"message": "m-4", "body": "agreed, @eval-agent"},
    }
    record_writer(tmp_path, "chat")(json.dumps(request))

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "mention", "agent-main mentioned you in a thread.")]


def test_a_direct_message_that_names_its_reader_is_a_direct_message_notice(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    notices = Notices(config, AGENTS)
    record_call(tmp_path, "chat", "agent-main", "send_dm", member="eval-agent", body="@eval-agent, a word")

    noticed = notices.read()

    assert noticed == [Notice("eval-agent", "dm", "agent-main sent you a direct message.")]


def test_a_mention_is_what_chat_reads_as_one() -> None:
    assert (MENTION.pattern, MENTION.flags) == (store.MENTION.pattern, store.MENTION.flags)
