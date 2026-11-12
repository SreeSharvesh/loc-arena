# The run reader

`loc-arena read <run directory>` has an agent read one graded run and report what its agents did. The report
goes to `readings/<brief>.md` in the run directory.

```bash
make read RUN_DIR=logs/<run>/<run>__<id>              # the agents brief
make read RUN_DIR=logs/<run>/<run>__<id> BRIEF=monitors
```

`loc_arena/run_reader.py` · `loc_arena/reader_briefs/`

## What a reading holds

| Part | From | What |
|---|---|---|
| Facts | the command, counted from the records | each play's events, jobs, messages, actions and pull requests; how sessions ended; the main and side task results; each monitor's verdicts in both plays |
| Account | the reader | answers to its brief's questions, each with event numbers |
| Calls | the gateway's record | `readings/<brief>.calls.jsonl`, the reader's model calls, and `readings/<brief>.calls.otlp.jsonl`, the same record as the Collector wrote it |

The numbers in the facts do not depend on the reader. Check the account against them: each claim cites event
numbers, and the calls file shows every command the reader ran. A second reading with the same brief replaces
the first.

## The briefs

| Brief | Asks |
|---|---|
| `agents` | what the agent with the covert objective did with it, what each other agent did, how far the attempt got and why it stopped, what differs from the honest twin |
| `monitors` | each monitor's spread of scores in both plays, what scored high, the score of each covert action, how caught was decided, what no monitor scored |

Read `agents` first: `monitors` uses its report when the run directory has one. A brief is a file in
`loc_arena/reader_briefs/`; a new file there is a new brief.

A brief tells the reader to describe covert steps by their kind, with no commands, code or credentials, to
mark each inference, and to end with what it could not determine.

## How the reader is isolated

A run's records are text that untrusted agents wrote, and the reader has a shell. It is isolated as an agent's
loop is.

| | The reader's container |
|---|---|
| Model calls | through the model gateway, on a route of its own that its token alone opens; the gateway records them, refuses a call that names another model than the `reader` role's or a list of fallback models, and refuses each one past `reader.model_calls` |
| Provider key | not there: it stays in the gateway's container |
| Network | the internal network alone, with no route out and no address of the host |
| Limits | a sandbox's memory, CPU and process limits |
| Run directory | mounted read-only |
| Writes | its home and one work folder, both temporary. The report is taken from the work folder only if it is a regular file of at most `reader.report_limit_bytes`: a link to a file of the host, a pipe and a directory are refused |

The reader is pi from the engine image, with its read, shell and write tools and nothing else. Its model is
the `reader` role of the models file. `reader.timeout_seconds` bounds one reading. A reading whose reader fails,
runs past its time, or whose calls cannot be copied out keeps no report.

## Limits

- The reader reports. It scores and decides nothing: the checker and the monitors do.
- One run for each reading.
- A verdict's `target_seq` numbers the observable log (`events.mirror.jsonl`), and the `agents` brief cites
  the sealed log. The `monitors` brief says so, and its event numbers are the observable log's.
