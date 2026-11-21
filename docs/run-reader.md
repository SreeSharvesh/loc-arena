# The run reader

`loc-arena read <run directory>` has an agent read one graded run, and code checks every claim it makes before
anyone reads it. A reading is a short account of a brief, or a short answer to one question.

<img alt="Data flow of a reading: the command gathers the run's facts, its events' words and its names from the run directory and gives the facts and the words to the isolated reader, which also reads the records read-only and writes its claims with their cites to answer.json; code checks every claim against the gathered evidence and writes only the claims that pass to readings/name.md, which the owner or an agent reads." src="figures/reading.svg" width="100%">

```bash
make read RUN_DIR=logs/<run>/<run>__<id>                         # the agents brief: what happened
make read RUN_DIR=logs/<run>/<run>__<id> BRIEF=monitors
make read RUN_DIR=logs/<run>/<run>__<id> ASK='Did any agent read its credentials in the twin?'
```

`loc_arena/run_reader.py` · `loc_arena/run_facts.py` · `loc_arena/reading_check.py` · `loc_arena/reader_briefs/`

## Insights

`make insights RUN_DIR=<run or logs directory>` turns each graded run into an attack chain across the team and
its interesting events, every item citing records that code checks. The reader's container makes one model call
on its gateway route, no agent: the call holds the run's digest (`run_insights/digest.py`), the attack play's
sessions in order (each turn's reasoning, calls and results) and the services' own records, with reasoning
clipped at 800 characters and calls and results at 300. The honest twin, the mirror log and the per-call model
records are left out. Each digest line has a short handle (`[r12]`) that a citation names and code maps back to
its record, since a model shortens long IDs. A run of 230k to 580k tokens costs $0.12 to $0.30 on the reader's
model and takes under a minute.

Each run gets `readings/insights.json` and `readings/insights.html`, a self-contained report that opens with the
insights and calls no model. The chain is framed by where the attempt started, and why that step is the start,
and how it ended; each cited step shows the agent's reasoning, its tool calls with their arguments, and each
result. All records opens on agent turns, one line each of reasoning, action and result.
A run is analyzed again only when its records, the reader's model, the brief (`reader_briefs/_insights.md`) or the
reader's settings change.

The report's page is the `run-insights` package (`viewer/`), which imports nothing of the engine, so it can be
published alone. `python -m loc_arena.insights_server <logs directory>` serves every bundle at
`http://127.0.0.1:8844` and can analyze one again from its page; a failed analysis shows the worker's last line
and keeps its output in `readings/insights.log`.

## What the reader is given

| File in its home | What |
|---|---|
| `facts.json` | every count and score of the run by key, computed by the command: each play's events by kind, tool and actor (`attack.agent.serving-agent.tool.bash`), the agents' own totals (`attack.team.kind.inference_call`), which actors used a tool, how each session ended, each monitor's verdicts, flagged verdicts and highest score, and each verdict's score; every scalar of `scores.json` (`scores.side_task.milestones.<id>.reached`), the milestones and `scores.json`'s keys; each agent's lead, covert milestones and repositories from the config; how often code saw each category of the behaviour taxonomy, out of its chances (`taxonomy.attack.access.read_secrets`). A count of zero is a fact, so absence can be cited |
| `checks.json` | the facts, words and names the checks read, and the reader's settings, so it can check its draft |
| `words.jsonl` | each event that holds words an agent wrote: the fields of `reader.word_fields` in its payload and arguments (a message, a ticket, a note, a pull request's text), and for a model call the reply of the agent's model |

It also reads the run directory itself. Plays are named `attack` (or `honest`) and `twin`.

## The checks

The reader writes its claims as JSON, each with what it cites: events (`attack:145` is seq 145 of the play's
sealed log, `attack-mirror:12` an event of its mirror log) or facts by key. A claim is kept only when it passes
every check.

| A claim is dropped when it | So that |
|---|---|
| cites what is no event of the logs and no fact, or cites nothing | every claim points at the record behind it |
| states a number, in digits or words (`two`, `both`, `twenty-three`), that is not a fact written as `{key}` | every count is the command's, filled in by code |
| gives a vague quantity (`mostly`, `many`, `some`, `repeatedly`) in place of a count | claims say how many, out of how many |
| quotes words no cited event holds verbatim | a quote is what an agent said or wrote |
| holds, quoted or not, anything but words of prose: a file name, a flag, a call, a path, a link, markup or a credential's shape | code, commands and credentials are described by kind, never shown |
| names in backticks what no record holds | no invented key, file or tool |
| is past `reader.claim_limit` or longer than `reader.claim_limit_characters`, or quotes more than `reader.quote_limit_characters` | a reading stays short |

A word of prose is letters with an apostrophe or a hyphen, an id such as `AURORA-3`, an abbreviation, a number or
punctuation. Section titles hold plain words alone, or are numbered.

Before it finishes, the reader runs the same checks on its draft (`python -m loc_arena.reading_check` on
`checks.json` in its home) and rewrites what they would drop. The command checks the answer again in any case.

The reading lists each dropped claim by its number and the check it failed, never its text. An error names where
the reader's JSON fails, never what it holds. An answer that fails a check, or that no kept claim supports, is
marked unsupported. A kept claim shows the events and facts it rests on.

## What a reading holds

| File in `readings/` | What |
|---|---|
| `<brief>.md`, or `ask-<hash>.md` for a question | a brief's reading opens with the command's table of facts, then the kept claims by section, each with its cites; a question's opens with the answer. Both end with what the reader could not determine and the checks |
| `<name>.unchecked.json` | what the reader wrote, before the checks: text untrusted agents' records shaped, never shown by the command |
| `<name>.calls.jsonl`, `<name>.calls.otlp.jsonl` | the reader's model calls, as the gateway and its Collector recorded them |

`make read ... ASK=...` prints its reading. A second reading of the same name replaces the first.

## The briefs

| Brief | Asks |
|---|---|
| `agents` | an overview of the run; what the agent with the covert objective did with it and who refused or questioned it; emergent behaviour and strategies; what differs from the honest twin |
| `monitors` | each monitor's spread of scores in both plays, what scored high, the score of each covert step, how caught was decided, what separates attack from twin |
| `ask` | one question, given by `ASK`: a short answer and the claims behind it |

`_answer.md` is what every brief shares: what the reader is given and the format the checks read. A new file in
`loc_arena/reader_briefs/` is a new brief. Read `agents` first: `monitors` uses its reading when the run
directory has one.

## Behaviour taxonomy

`configs/taxonomy.yaml` is one versioned file of the behaviour a run is classified by, so a claim says how often
something happened out of how many chances. Each category has a group, a one-line definition, an example, its
unit of chance and who finds it.

| Group | Categories | Found by |
|---|---|---|
| `mast` | the 14 failure modes of MAST: specification, inter-agent misalignment, task verification | the reader; step repetition by code |
| `covert` | acted on the covert brief, framed it as ordinary work, refused, refused and escalated, walked it back | the reader |
| `access` | read its credentials, worked outside its scope, read the grader | code, but reading the grader |
| `control` | hit its call budget, blocked by a control | code |
| `failure_main`, `failure_side`, `failure_monitor` | why the main task, the side task or the monitors fell short | code, but a covert step a monitor left unflagged |

A unit is an agent, a session, an action, the main task, a milestone or a verdict: an agent or a session counts
once, the rest each time. A category is counted only in the plays it applies to (`applies_to`): every play, the graded
play, the attack play, or the honest play, which is the twin or an honest run's own; a play it does not apply to
has no fact of it. The reader's `taxonomy.json` holds each category's definition and the instances code observed,
with their seqs; the reader names a category it judges by its id. `loc_arena/taxonomy.py` holds a detector for each
category code finds; a new category names one, or is the reader's. A category moves group, splits or is renamed by editing the file and raising
its `version`. `reader.repeat_at_least` sets how often one command makes a step repetition.

## Accuracy

`tests/integration/test_live_reader_accuracy.py` asks the reader each of the 35 questions in
`tests/_reader_questions.py` about the stored run, one reading each, and grades each answer against a value
computed from the run's raw records apart from the reader's facts. The reader's model answers at least 34; a
reading that fails counts as a wrong answer. It spends model calls, so it runs only live:

```bash
LOC_ARENA_LIVE=1 uv run pytest -s tests/integration/test_live_reader_accuracy.py
```

## How the reader is isolated

A run's records are text that untrusted agents wrote, and the reader has a shell. It is isolated as an agent's
loop is.

| | The reader's container |
|---|---|
| Model calls | through the model gateway, on a route of its own that its token alone opens; the gateway records them, refuses a call that names another model than the `reader` role's or a list of fallback models, and refuses each one past `reader.model_calls` |
| Provider key | not there: it stays in the gateway's container |
| Network | the internal network alone, with no route out and no address of the host. The gateway beside it tunnels through `reader.outbound_proxy` where that is set, so on a machine whose way out is its own proxy a reading's relay listens apart from a play's |
| Limits | a sandbox's memory, CPU and process limits |
| Run directory | mounted read-only |
| Writes | its home and one work folder, both temporary. Its answer is taken from the work folder only if it is a regular file of at most `reader.report_limit_bytes`: a link to a file of the host, a pipe and a directory are refused |

The reader is pi from the engine image, with its read, shell and write tools and nothing else. A reading
builds no image: it runs the one the checkout's last run or `make images` built. Its model is
the `reader` role of the models file. `reader.timeout_seconds` bounds one reading. A reading whose reader fails,
runs past its time, whose calls cannot be copied out, or whose answer is not the JSON its brief asks for keeps
no reading. Its output is never shown: only the checked reading is.

## Limits

- The reader reports. It scores and decides nothing: the checker and the monitors do.
- One run for each reading.
- The checks hold what a claim cites, quotes, counts and names to the records. They cannot tell whether a
  true quote or count supports what the claim says of it: read a claim with its cites.
- A verdict's `target_seq` numbers the observable log, so the `monitors` brief cites `attack-mirror:<seq>`.
