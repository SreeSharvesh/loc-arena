# The forge

Status: planned. Slice 1 is in progress.

The forge is Meridian's code host, as GitHub is a company's. Today it is a live service whose contents are
simulated: its repos are generated histories rather than the code agents edit, a pull request is a record holding
a free-text diff, CI reads a status stored on the pull request, and the reviewer is a keyword rule. Agents share
one checkout with no git, so every edit is every agent's at once, and nothing an agent merges reaches anything.

This plan makes the forge a real git host. Each agent works in its own clone, a teammate's change reaches it only
once merged and pulled, and what is merged on `main` is what is graded and, later, what is deployed.

## Plan

| Slice | Adds | Status |
|---|---|---|
| 1. Clone and pull | the forge hosts a bare repository per company repo, seeded from the company's code, and serves git's smart HTTP protocol under IAM: `read` lists refs and fetches; each agent clones into its own working copy | in progress |
| 2. Push | pushing branches, `push` under IAM; `main` refuses every push; each push recorded | planned |
| 3. Pull requests and merge | `open_pr` on a pushed branch with the diff git computes; `merge` under IAM, author not merger on a control-plane repo, the reviewer on the real diff, git's own merge onto `main` | planned |
| 4. Grading from `main` | the forge's repositories copied out after play, the main task graded on merged `main`, the honest twin's forge seeded afresh | planned |
| 5. Real CI | each repo's suite run on a pull request in an isolated container, never in the forge's | planned |

## Rules

- IAM is the forge's only authority: no forge users, keys or per-repo permission table. An off-the-shelf forge
  (Gitea, Forgejo) keeps its own users and permissions, so the forge serves git with git's own `http-backend`
  behind the shared IAM integration instead.
- The caller is the principal IAM names. A commit's author is whatever the committer typed; no record and no
  check reads it as an identity.
- Agent code never runs in the forge's container: it holds the forge's IAM credential and its records.
