# Pull request and shipping checks

Every pull request runs the existing Ruff/format/typing/test gates and a separate fast file-size job. Only
added, modified and renamed text files are checked, using their entire contents including comments and blank
lines. Compare with the merge ancestor of the PR base: a file at or below 700 lines must remain at or below
700; a file already larger may shrink or stay the same but cannot grow. New files have a 700-line cap.
Untouched files are excluded. Renames retain the old path's baseline; copies are new files. Binary files,
symlinks and generated `uv.lock` are excluded. Private untracked files are never scanned. There is no list
of legacy filename exceptions; a file that shrinks to 700 automatically loses its larger allowance next PR.

CI fetches the base history and passes the PR target SHA (or prior main commit for a push). Locally run
`python3 scripts/check_file_size.py --base origin/main` after fetching; `make gates` runs the same rule.
For a stacked PR use its target branch with `make gates FILE_SIZE_BASE=<target-ref>`. Tracked staged and
working-tree edits are included in local checks. Missing base history fails rather than silently checking
the wrong set of files.

## CodeRabbit must return a clean latest-commit review

The `CodeRabbit clean review` check requires a successful CodeRabbit status, a CodeRabbit review associated
with the current PR head commit, zero actionable comments in that head's reviews, no new inline findings on
that head, and no unresolved older CodeRabbit review threads. A successful CodeRabbit status alone is not
sufficient: CodeRabbit can finish successfully while reporting findings. Blank completed review bodies with
no inline findings are accepted; positive reported findings fail even if a reply marks them resolved.

Fix verified findings, explain any rejected finding on its thread, resolve addressed threads, then obtain a
fresh CodeRabbit review on the new head. Do not disable the bot, forge a review or edit its finding count to
pass. This check does not assess the correctness of a fix or cover non-inline nitpicks in summary issue comments;
normal review remains necessary. Human review requirements are separate.

The gate refreshes on PR opening/new commits and CodeRabbit status updates. Review-event triggers are
excluded because their workflow YAML can come from the PR merge ref, before any trusted checkout occurs.
After resolving a thread without a new commit/status event, post `/recheck-coderabbit` as a new PR comment.
That issue-comment event runs default-branch workflow code; it cannot select an arbitrary execution ref.
Branch-selectable `workflow_dispatch` is deliberately absent from this privileged workflow.
GitHub does not provide a review-thread-resolution trigger to this workflow. Pending, unavailable or stale
review metadata fails closed. The workflow and scripts execute only from the trusted default branch; no
pull-request code runs with its check-writing token. It posts a check directly on the current PR head SHA.
It becomes active after the workflow lands on the default branch.

CodeRabbit also runs its request-changes workflow: it requests changes while it has unresolved findings and
approves only when the latest commit is reviewed, its required threads are resolved and no pre-merge check fails.
The author cannot approve or resolve for it, or override a failing pre-merge check. Its approval does not replace
a human review.

## Maintainer setup required before this blocks shipping

A repository administrator must configure main's branch protection/ruleset to require:

- `file size (700 lines)`
- `ruff, ty, pytest`
- `CodeRabbit clean review`
- CodeRabbit's approval, and at least one human approval
- Conversation resolution for all review threads, and no bypass if the policy is truly universal.

Use equivalent requirements on any shipping/release branch. The current automation account has push access
but no admin/maintain access, so these settings have not been changed. Without required checks, CI reports
failures but cannot prevent a maintainer from merging. Confirm bot automatic reviews cover the repository;
when CodeRabbit pauses reviews, explicitly request another review rather than bypassing the fresh-review gate.
