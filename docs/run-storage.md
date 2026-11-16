# Run storage

<img alt="Data flow of a publish: the run bundle is scanned; a real secret stops it with each file and line named; otherwise its readable files are committed to the storage repository and the whole bundle is released on that commit, each only when absent; a reader fetches both back with git clone and gh release download." src="figures/run-storage.svg" width="100%">

```sh
make publish-run RUN_DIR=logs/<slug>/<run name>
```

A run bundle lives on the machine that played it. `make publish-run` copies a graded one to the storage
repository that the run config's `publish.repository` names; `env.default.yaml` sets the private
`Antonio-Tresol/loc-arena-runs`. It needs git and `gh` signed in with push access to that repository
(`gh auth setup-git` lets git use `gh`'s sign-in).

## What is stored where

| Where | What |
|---|---|
| `runs/<date>/<config>/<run id>/` on `publish.branch` (`main`) | the files `publish.readable_files` names, when present: `scores.json`, `config.resolved.yaml`, `cost.json`, the explorer's pages and the readings; and `bundle.txt`, which names the release |
| the release tagged `<date>-<config>-<run id>` | the whole bundle as one asset, `<date>-<config>-<run id>.tar.gz` |

`<config>` is the run config's name, which the run writes as `config_name` in `scores.json`, so configs that
share a slug, such as `aurora-teams` and `aurora-efficiency`, are filed apart. `<date>` is the day the run
finished, in UTC, and `<run id>` is the end of its name. A bundle without `config_name` is refused.

## The scan

Nothing is written until a scan of every line of every file, the members of a zip such as the Inspect log
included, finds no real secret. It refuses, naming each file and line:

- a key of a form in `publish.key_patterns`: the provider's, and GitHub's tokens;
- an `Authorization` credential that is not in-world;
- the value of a variable of the machine's environment or of the checkout's `.env` whose name says it holds a
  secret (`publish.environment_name_pattern`), when it is at least `publish.environment_value_min_length` long;
- a link, whose target the scan does not read.

The company's credentials are minted for each run as 32 random URL-safe bytes, and agents' git configs carry
them in `Authorization` headers. A credential of that form (`publish.in_world_token_pattern`) is part of the
run and stays. It is still refused when it equals a value of the machine's environment.

## Nothing is replaced

The commit and the release are each written only when absent, so a second publish of a run writes nothing,
says the run is already published and prints the link. A publish that stopped between the two writes the part
that is missing. A release stays a draft until its asset is uploaded, so a published release always holds its
bundle. Each git or `gh` command ends within `publish.command_seconds`.

## Fetch a run back

```sh
gh release download <date>-<config>-<run id> --repo Antonio-Tresol/loc-arena-runs
tar -xzf <date>-<config>-<run id>.tar.gz -C logs
make explore LOGS=logs/<run name>
```

The readable files need no download: open `runs/` on GitHub, or clone the repository. The records hold
untrusted agents' text, attack attempts in a simulated company among them; read them with the
[run reader](run-reader.md) where you can.
