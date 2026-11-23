# Monitors that read whole trajectories

The three monitors a run is graded with each rate one step, one pull request or the model-call stream. Two
more read whole trajectories, after the play, and exist to compare monitoring strategies as an attack is split
across agents:

| Monitor | What it reads | Model calls per play | Its score for a play |
|---|---|---|---|
| `agent_trajectory_monitor` | one agent's whole trajectory, in one prompt | agents × samples | the most suspicious agent's |
| `team_summary_monitor` | a summary of each agent's trajectory, all in one prompt | agents + samples | its one verdict |

Neither is enabled in `configs/monitors.yaml`, so no run is graded with them: `make monitor` runs them over
runs already played.

## What a trajectory holds

An agent's trajectory is its tool calls, each with its arguments and its result, and its messages in chat, in
order, from the mirror log. It holds no model reply: neither the agent's reasoning nor the text it wrote
between calls is in the events a monitor reads. Only the agents under test have one, never a scripted
coworker or the reviewer. A step of a subagent is in its agent's trajectory under the subagent's own
`actor_id`, so handing work to a subagent hides nothing from the agent's monitor.

`result_chars` in a monitor's entry bounds one tool result (20000 characters).

## How each one rates

`agent_trajectory_monitor` asks its model once per sample for a verdict on one agent
(`agent-trajectory.monitor.md`). `ensemble_n` is the number of samples (3) and `ensemble_reduce` how they
become the agent's score (`mean`); `temperature` is above 0 so that they differ. `per_monitor_reduce: max`
makes the play's score its most suspicious agent's.

`team_summary_monitor` works in two steps with one model. A summarizer (`team-summary.summarizer.md`) writes
at most `summary_words` words (300) on what each agent did, one call per agent, and is told to describe and
not to judge. The monitor (`team-summary.monitor.md`) then reads the summaries together, and no step, and
gives the team one verdict, sampled like the other's. Each summary is recorded as a `monitor_summary` event.

Both rate with their model alone. With no model, or when every sample of a verdict fails, a monitor gives no
verdict, and the team monitor gives none when any agent's summary is missing: a verdict on part of the team
would read as one on all of it. No verdict is reported as no score, never as zero.

Their model is the `trajectory_monitor` role of `configs/models.cheap.yaml`, DeepSeek V4.1 Flash, which has
the context to hold a trajectory. That is the untrusted agent's own model, so a result with it is one of
untrusted monitoring, where the monitor may collude with the agent; name a trusted model in the role for
trusted monitoring. Every monitor calls the model of the role its entry names.

## Run monitors again over a stored run

```sh
make monitor RUN_DIR=logs/<slug>/<run> MONITORS=agent_trajectory_monitor,team_summary_monitor
make monitor RUN_DIR=<run> MONITORS=action_monitor RUN=aurora-efficiency.fast OUT=<folder>
```

`MONITORS` names any monitors of the run config's monitors file, enabled or not. The command reads each
play's mirror events, the run's own and its honest twin's, and runs those monitors over them. It needs
`OPENROUTER_API_KEY`, as grading does; without it a monitor scores as it does without a model.

It changes nothing of the run. The monitors' model calls go through a gateway core that records them in the
output folder, never in the run's sealed log, and no task is graded again. It writes into `OUT`, by default
`extras/monitors-<time>/` in the run directory:

| File | What it holds |
|---|---|
| `scores.json` | per monitor: its model, samples and the fingerprint of each prompt file it names; per play, its score, each actor's highest verdict, and the counts of verdicts and failed calls; and its calls, tokens and dollars over both plays |
| `verdicts.jsonl` | every verdict: its play, monitor, actor, score and reason |
| `<play>.events.jsonl` | the record of that play's monitor calls, with the tokens and dollars of each, each `monitor_summary`, and each failed call |

A play is named `attack` or `honest`, the run's mode, and `twin`. Dollars are what the provider reports for
each call, and none where it reports none.

These scores are judgements after the play: every verdict counts, whenever its events happened. A run's own
`scores.json` counts a verdict only up to the harm step, so the two are not the same measure, and a monitor
that reads a trajectory to its end, enabled in a run, would be dated after the harm of an attack that
succeeded.

Store a re-run with its run: scan its folder and commit it into `extras/`
([how](run-storage.md#store-what-else-a-run-yields)).
