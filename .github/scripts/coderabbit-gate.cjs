// Runs only from trusted default-branch code; never execute the pull request's checkout.
const bot = login => login === 'coderabbitai[bot]' || login === 'coderabbitai';

// CodeRabbit posts a review only when it has something to say, so a clean review of the head is its report on that
// commit alone: "Review completed" (not "Review skipped") means it reviewed this one. Approving the pull request
// replaces that description with "Review approved", which also means it did.
const REVIEWED = new Set(['Review completed', 'Review approved']);

// CodeRabbit reports on a commit with a check run whose summary starts "Result: Success - Review completed", or, as
// it did before, with a commit status. Only CodeRabbit's app can write its check run.
function report(statuses, checks) {
  const check = checks.find(c => c.name === 'CodeRabbit' && c.app?.slug === 'coderabbitai');
  const result = /^Result: .*? - (.*)$/m.exec(check?.output?.summary || '');
  if (check) return { status: check.conclusion, description: result?.[1] };
  const status = statuses.find(s => s.context === 'CodeRabbit');
  return { status: status?.state, description: status?.description };
}

function evaluate({ head, status, description, reviews, comments, unresolved }) {
  if (status !== 'success' || !REVIEWED.has(description)) {
    return { ok: false, reason: 'CodeRabbit has not completed a review of the latest commit.' };
  }
  const current = reviews.filter(r => bot(r.user.login) && r.commit_id === head && r.state !== 'PENDING');
  const flagged = current.some(r => /Actionable comments posted:\s*[1-9]\d*/i.test(r.body || ''));
  const inline = comments.filter(c => bot(c.user.login) && !c.in_reply_to_id && c.original_commit_id === head);
  if (flagged || inline.length) return { ok: false, reason: 'Latest-commit review has findings; fix and obtain a fresh clean review.' };
  if (unresolved) return { ok: false, reason: `${unresolved} CodeRabbit review thread(s) remain unresolved.` };
  return { ok: true, reason: 'Fresh CodeRabbit review: zero actionable findings and no unresolved threads.' };
}

async function run({ github, context }) {
  const repo = context.repo;
  let pulls = context.payload.pull_request ? [context.payload.pull_request] : [];
  if (context.eventName === 'status' || context.eventName === 'check_run') {
    const name = context.payload.context ?? context.payload.check_run?.name;
    const sha = context.payload.sha ?? context.payload.check_run?.head_sha;
    if (name !== 'CodeRabbit') return;
    pulls = await github.paginate(github.rest.repos.listPullRequestsAssociatedWithCommit, {
      ...repo, commit_sha: sha, per_page: 100,
    });
    pulls = pulls.filter(p => p.state === 'open' && p.head.sha === sha);
  }
  if (context.eventName === 'issue_comment') {
    if (!context.payload.issue?.pull_request || context.payload.comment?.body !== '/recheck-coderabbit') return;
    pulls = [(await github.rest.pulls.get({ ...repo, pull_number: context.payload.issue.number })).data];
  }
  for (const pull of pulls.filter(p => p.state === 'open')) {
    const number = pull.number;
    const head = (await github.rest.pulls.get({ ...repo, pull_number: number })).data.head.sha;
    let result;
    try {
      const [reviews, comments, statuses, checks] = await Promise.all([
        github.paginate(github.rest.pulls.listReviews, { ...repo, pull_number: number, per_page: 100 }),
        github.paginate(github.rest.pulls.listReviewComments, { ...repo, pull_number: number, per_page: 100 }),
        github.paginate(github.rest.repos.listCommitStatusesForRef, { ...repo, ref: head, per_page: 100 }),
        github.paginate(github.rest.checks.listForRef, { ...repo, ref: head, check_name: 'CodeRabbit', filter: 'latest',
          per_page: 100 }),
      ]);
      let unresolved = 0, cursor = null;
      do {
        const response = await github.graphql(`query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) { pageInfo { hasNextPage endCursor }
              nodes { isResolved comments(first:1) { nodes { author { login } } } }
            }
          } }
        }`, { owner: repo.owner, repo: repo.repo, number, cursor });
        const threads = response.repository.pullRequest.reviewThreads;
        unresolved += threads.nodes.filter(t => !t.isResolved && bot(t.comments.nodes[0]?.author?.login)).length;
        cursor = threads.pageInfo.hasNextPage ? threads.pageInfo.endCursor : null;
      } while (cursor);
      result = evaluate({ head, ...report(statuses, checks), reviews, comments, unresolved });
    } catch {
      result = { ok: false, reason: 'Could not verify CodeRabbit metadata; retry this check.' };
    }
    await github.rest.checks.create({ ...repo, name: 'CodeRabbit clean review', head_sha: head,
      status: 'completed', conclusion: result.ok ? 'success' : 'failure',
      output: { title: result.ok ? 'Clean review' : 'Clean review required', summary: result.reason } });
  }
}

module.exports = { evaluate, report, run };
