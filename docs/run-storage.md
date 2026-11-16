# Run storage

<img alt="Data flow of a publish: the run bundle is scanned; a real secret stops it with each file and line named; otherwise one commit, only when the run is absent, adds its readable files and the whole bundle as an xz archive, split into parts over 95 MB, with bundle.txt; a reader fetches them back with a sparse checkout or a raw download." src="figures/run-storage.svg" width="100%">

```sh
make publish-run RUN_DIR=logs/<slug>/<run name>
```

A run bundle lives on the machine that played it. `make publish-run` commits a graded one to the git
repository that the run config's `publish.repository` names; `env.default.yaml` sets the private
`Antonio-Tresol/loc-arena-runs`. It needs git with push access to that repository.

## What is stored where

One commit per run adds the folder `runs/<date>/<config>/<run id>/` on `publish.branch` (`main`):

| File | What it is |
|---|---|
| the files `publish.readable_files` names, when present | `scores.json`, `config.resolved.yaml`, `cost.json`, the explorer's pages and the readings |
| `bundle.tar.xz` | the whole bundle, less what is rebuilt from it (below); over `publish.part_bytes` (95 MB) it is split into `bundle.tar.xz.part-00`, `.part-01` and on, as GitHub refuses a file over 100 MB |
| `bundle.txt` | the archive's size and SHA-256, its parts and how to join them, and what was left out |

`<config>` is the run config's name, which the run writes as `config_name` in `scores.json`, less the
`.local` of a machine's own config: configs that share a slug, such as `aurora-teams` and
`aurora-efficiency`, are filed apart, and `aurora-efficiency.fast.local` is filed as `aurora-efficiency.fast`.
`<date>` is the day the run finished, in UTC, from the episode's own record, and `<run id>` is the end of its
name. A bundle without `config_name`, or without that record, is refused.

## What the archive leaves out

The archive leaves out each play's `checkout`, the trees of `main` its main task is graded on, when
`rebuild_checkout` makes it again byte for byte from the play's `forge_history/<repo>.bundle`: the commit of
`main`, unpacked as the run unpacked it. The publish rebuilds each checkout before it leaves it out, so a
checkout whose history is unread, or that differs from it, stays in the archive. The caches grading wrote in a
checkout hold no trace and are not rebuilt. `publish.leave_out_rebuilt_checkouts` turns this off.

A publish neither scans nor archives what it leaves out. Every trace stays: the sealed and mirror logs, the
gateway's call record in both forms, the Inspect log, the transcripts, Phoenix's spans, the services' logs and
records, each agent's checkout history, the forge's repositories and the deployed trees. xz finds what each
model call repeats of its conversation, which gzip's 32 KB window cannot: a full `aurora-efficiency` run of
679 MB is 151.5 MB as gzip and 10.8 MB as published.

## The scan

Nothing is written until a scan of every line of every file finds no real secret. It reads the members of a
zip, such as the Inspect log, every object of a git store or a git bundle, such as the forge's repositories
and their history, and every row of a SQLite file. It refuses, naming each file and line:

- a key of a form in `publish.key_patterns`: the provider's, and GitHub's tokens;
- an `Authorization` credential with a digit that is not in-world, after a scheme or none, written as a
  header, a mapping, a pair or URL-encoded, quoted or escaped as JSON within JSON;
- the value of a variable of the machine's environment or of the checkout's `.env` whose name says it holds a
  secret (`publish.environment_name_pattern`), raw or escaped as JSON, and the password of any URL a variable
  holds, when it is at least `publish.environment_value_min_length` long;
- anything the scan cannot read. A link's target is read as its line, as the archive keeps the link.

The company's credentials are minted for each run as 32 random URL-safe bytes, and agents' git configs carry
them in `Authorization` headers. A credential of that form (`publish.in_world_token_pattern`) is part of the
run and stays. It is still refused when it equals a value of the machine's environment.

## Nothing is replaced

A run whose folder exists is not written again: the command says it is already published and prints the
folder's link. Another run under the same label is refused, naming the run already filed there, and so is a
run with a file that changed after its scan began. Each git command ends within `publish.command_seconds`.

## Fetch a run back

Take the run's folder alone with a sparse checkout, then join, check and unpack it into a loc-arena
checkout's `logs/<run slug>/`, the `run_slug` its `scores.json` gives:

```sh
git clone --filter=blob:none --sparse https://github.com/Antonio-Tresol/loc-arena-runs
git -C loc-arena-runs sparse-checkout set runs/<date>/<config>/<run id>
cd loc-arena-runs/runs/<date>/<config>/<run id>
cat bundle.tar.xz.part-* > bundle.tar.xz   # only when bundle.txt lists parts
sha256sum bundle.tar.xz                    # the digest bundle.txt gives
tar -xJf bundle.tar.xz -C <loc-arena>/logs/<run slug>
```

One file also comes as a raw download, up to 100 MB:

```sh
gh api repos/Antonio-Tresol/loc-arena-runs/contents/runs/<date>/<config>/<run id>/bundle.tar.xz \
  -H "Accept: application/vnd.github.raw" > bundle.tar.xz
```

The unpacked run opens in `make explore` and `make read RUN_DIR=logs/<run slug>/<run name>`. To grade it again,
rebuild each play's checkout first:

```sh
uv run python -c 'import sys; from pathlib import Path; from loc_arena.run_publish import rebuild_checkout
for play in ("episode", "honest_cal"): rebuild_checkout(Path(sys.argv[1]) / play, Path(sys.argv[1]) / play / "checkout")' \
  logs/<run slug>/<run name>
```

The records hold untrusted agents' text, attack attempts in a simulated company among them; read them with the
[run reader](run-reader.md) where you can.
