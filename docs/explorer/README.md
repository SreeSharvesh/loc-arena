# Run explorer

A static HTML page for reading a run: every agent side by side, round by round, with each monitor's verdict next
to the step it judged, who said what to whom, and your own notes on any passage. It needs no server: build it,
then open the file.

## Open it

```sh
make explore                 # every run under logs/, opens the index in your browser
make explore LOGS=path/to/runs
```

Or call the CLI directly, on one run bundle or on a folder of them:

```sh
uv run python -m loc_arena.cli explore logs/<run_slug>/<run_name>   # one run  -> <bundle>/explorer.html
uv run python -m loc_arena.cli explore logs                         # a folder -> logs/explorer/index.html
uv run python -m loc_arena.cli explore logs --out /tmp/site --no-open
```

A run needs its Inspect log (`<run_name>.eval`) with the agent spans, written when `logging.agent_transcript` is
on (the default). A folder's index also lists a run without one, with no link to a page.

## The honest twin

An attack run also plays its honest twin, and the Inspect log holds it as a second sample (`honest_cal`). Each such
run gets a second page beside its own, `<run_name>__honest_twin.html` (`explorer__honest_twin.html` for a single
bundle). **Attack play | Honest twin** in the top bar switches between them, keeping the view and filters. The twin's
page is its transcript with its own verdicts, which grading keeps in `scores.json`'s `honest_twin`, and its model
reasoning. Every step there scored above the threshold is a false alarm. It has no outcome or side-task evidence of
its own: the twin is played to set the audit threshold. Its **Monitors on the twin** view, and a section of the run's
**Outcome and monitors**, set each monitor's score on the run beside its score on the twin, and the run's overall
suspicion beside the twin's, which is the bar the run is caught above. A bundle graded before grading kept the twin's
verdicts shows its twin without them, and says so.

A pill with a ring is a verdict exactly at the audit threshold: one of the twin's scores that set it, so not a flag,
but not clear either.

## All runs

![The index of every run in a folder](01-index.png)

One row per run: main-task score, whether the side task landed, whether a monitor caught it and which, the
highest score each monitor gave, and how many steps were flagged, covert, or covert and missed. The landscape above
the table crosses attacker model with monitor model. Click a run to open it; its breadcrumb leads back here.

## Transcript

![The transcript grid: one lane per agent and per monitor, one row per round](02-transcript.png)

Monitors on the left, then one lane per agent, then World (the scripted coworkers and the platform). Each row is a
round. A card is one agent turn: the tool it called, the most telling argument, and the start of the result.

- **Magenta** is a step a monitor flagged, **teal** a step monitors scored at or below the audit threshold,
  **orange** (the dot and the tags) covert: the sealed verifier cites the step as side-task evidence, or the step called an attack tool.
- A verdict sits on the step it judged. The harness numbers events twice (sealed and mirror logs); verdicts point
  at mirror events, and the page maps them back to the step that caused them. The verdicts about the whole run sit
  in the *whole run* row.
- In each round, a monitor column shows the verdicts above the audit threshold as cards and collapses the rest to
  a count ("7 at or below 0.2"), so a column of zeros does not bury the flags.

Click a card to open it in place:

![A card expanded in the grid, with a highlighted passage and its note](03-expanded-card.png)

The expanded card shows the model's reasoning, the whole action, the result, each monitor's reason, and your
notes under the passages they quote. `open ↗` moves it to the side panel. Escape collapses every open card.

## A step in the side panel

![The side panel with a note in the margin](04-side-panel.png)

The panel shows the step at full width. When it is wide enough (drag its divider), notes move to the margin
beside their passage. When it is narrow, each note sits under the block it quotes. `J` and `K` move to the next
and previous step, and `R` marks a step read.

![The folded sections: raw reply, context, side effects](05-folds.png)

Below the action, each part of the turn is folded under an icon, so you learn where to look:

| Fold | What it holds |
|---|---|
| Reasoning | the model's reasoning on that call, collapsed, when the model returned some |
| Raw reply | the model's reply as returned, which the action above was parsed from |
| Context the model saw | the whole prompt of that turn |
| Side effects of this turn | the events the turn caused on the platform |

The reasoning comes from the call log the gateway of a stack run writes beside the run (`logs/gateway/`), where
each reply is recorded as OpenRouter returned it. Each recorded reply is matched to its sealed `inference_call` by
its `output_fp`. The expanded card shows it collapsed above the action. An in-process or scripted run, a model with
reasoning off, or a bundle moved away from its call log has none, and the page says so. Only the page reads it:
the monitors never do.

## Communication

![Communication on a real run: one message to the lead, and four sends the board refused](06-communication.png)

Everything said in the run: chat messages between agents, messages chat refused, and posts by the environment
(Slack, PRs, reviews, tickets, wiki); an earlier run shows its board messages and scratch notes the same way. The grid counts who talked to whom. Click a count to list only
those. Each entry says who read it:

- another agent, when a later prompt of that agent contains the text;
- a monitor, when a monitor's prompt contains it or a verdict was made on it;
- *no one*, when neither happened.

A text too short to find reliably is marked as such. A refused message was never delivered. The run above is an
earlier one: the job-service agent's one message reached the lead, and the board of that time refused its four
messages to other service agents.

![Communication on an invented variant of the same run](07-communication-synthetic.png)

**This one is synthetic.** It is the same run with invented messages between agents added, each marked read by its
recipient, to show how the view reads when agents do talk. None of it happened. The banner at the top of the page
says so.

## Outcome and monitors

![Outcome, the covert-by-flagged table, and each monitor](08-outcome.png)

The run's result, then the covert steps crossed with the flagged ones: covert and flagged, covert and missed,
flagged but not covert, and neither. Click a count to list those steps. A step counts as flagged when a verdict
on it scores **strictly above** the audit threshold, as the harness counts it. Below that, each monitor's number
of verdicts, how many landed on a step, how many flagged, its peak score, and its top reason.

## Search steps

![Searching the steps for a word](09-search.png)

Filter by lane, tool, monitor outcome and covert, or search the text of actions, replies, results and monitor
reasons. The filters live in the URL, so a link reopens the same selection. *Random sample* picks steps to read
cold. *copy selection* copies the id and facts of every step in the selection as JSON, to hand to an agent.

## Your notes

![The notes dialog](10-notes.png)

Select any words in an action, reply, result or event and pin a note to them, or write a note on the whole step.
Notes are kept in this browser, per run, and survive a rebuild of the page, because a note finds its passage by
the words it quotes, not by position. *Save all notes to a file* writes `loc-arena-notes-<run>.json`. Loading a
file, or dropping it anywhere on the page, merges it with what is there, so two people's notes combine.

## Themes

![The same page in the dark theme](11-dark.png)

Auto (follows the system), Light, Dark, and text size (`A−` / `A+`), all remembered per browser.

## Credit

The page design and the annotation model come from the Odd Number trace explorer
([Antonio-Tresol/odd-number-reward-hacking](https://github.com/Antonio-Tresol/odd-number-reward-hacking), MIT),
adapted to multi-agent runs.
