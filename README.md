# agent-orchestra

`agent-orchestra` is a lightweight local workflow orchestrator for development
and review agents.

The project assumes a Git worktree workflow. It coordinates bounded agent
invocations while keeping orchestration state, review artifacts, and lifecycle
authorization explicit.

## Participants

- A development agent implements changes and addresses review feedback.
- A reviewer agent evaluates the current diff and produces a Markdown review
  artifact.

## Supported scenarios

- Before a development agent commits its work, a reviewer agent reviews the
  diff and hands structured feedback back to the developer. The cycle repeats
  until the review is approved, after which the workflow may request permission
  to commit and open a pull request.
- A CLI command enqueues a pull-request review from its URL.

The local development/review cycle is the first implementation target. Remote
pull-request enqueueing is part of the intended interface but is not implemented
yet.

## Practical workflow today

Agent-orchestra does not require repos to be registered. It also does not yet
dispatch agents or advance queued runs automatically. The implemented CLI can
install the role skills, capture an existing uncommitted diff in SQLite, and
show captured runs. The user must currently create the task worktree, invoke
each agent role, and authorize lifecycle actions manually.

### Install the role skills once

From an agent-orchestra checkout, install both bundled skills for Codex and
Claude Code:

```shell
mise run agent-orchestra -- skills install \
  --agent all \
  --skill agent-orchestra-developer

mise run agent-orchestra -- skills install \
  --agent all \
  --skill agent-orchestra-reviewer
```

Repeat installation after a bundled skill version changes. The installer
upgrades an unchanged managed installation and preserves locally modified
installations.

### Start new work in any repo

A clean repo does not need an enqueue or registration step. Create or reuse an
isolated linked worktree according to the repo's worktree policy, start a coding
agent in that worktree, and give it an explicit development assignment:

Invoke a skill with `$agent-orchestra-developer` or
`$agent-orchestra-reviewer` in Codex, and `/agent-orchestra-developer` or
`/agent-orchestra-reviewer` in Claude Code. The prompt templates below use a
runtime-neutral placeholder for that invocation.

```text
<invoke the agent-orchestra-developer skill>

Objective: <describe the requested change and acceptance criteria>.

Work only in the current worktree.
Allowed actions: edit files and run local validation.
Leave validated changes uncommitted and return a ready_for_review handoff.
```

Name additional allowed actions only when they are intended. Editing does not
authorize a commit, and a commit does not authorize a push or pull request.

### Review the resulting diff

After the developer returns a handoff, start a separate agent invocation in the
same worktree and assign only the reviewer role:

```text
<invoke the agent-orchestra-reviewer skill>

Review the exact uncommitted diff in the current worktree against <base SHA>.
Objective: <repeat the original objective and acceptance criteria>.
Do not modify the worktree or perform remote actions.
Write the review to <absolute-review-artifact-path>.
```

Keep the review artifact outside the target worktree when it is workflow
evidence rather than repo documentation. For a repeat review, provide the prior
artifact and the exact current diff scope again.

### Remediate review findings

Return actionable feedback to a development agent rather than asking the
reviewer to fix its own findings:

```text
<invoke the agent-orchestra-developer skill>

Objective: <repeat the original objective and acceptance criteria>.
Address every finding in <absolute-review-artifact-path>.
Evaluate each finding and report it as addressed, rejected with rationale, or
blocked on a decision.
Run the repo's complete validation and leave changes uncommitted.
```

Repeat the review and remediation steps until the exact diff has no actionable
findings.

### Authorize lifecycle actions separately

After approval, give separate instructions for each intended state-changing
operation. For example:

```text
Commit the validated changes with a Conventional Commit.
```

Then, only when publication is intended:

```text
Push the committed branch and create a pull request. Do not merge it.
```

Finally, only after checking the live pull-request head, checks, approvals, and
mergeability:

```text
Merge <pull-request URL> and verify the resulting default-branch commit.
```

### Capture existing dirty work when useful

`enqueue-local` records the current uncommitted diff for one repo.
`enqueue-locals` scans immediate child repos, records dirty ones, and skips
clean ones. These commands do not start development or review, and no worker
currently consumes a run left in `queued`:

```shell
mise run agent-orchestra -- enqueue-local /path/to/dirty/repo
mise run agent-orchestra -- enqueue-locals ~/Projects
mise run agent-orchestra -- status
```

The intended automatic orchestration described below is the target contract,
not the current CLI behavior.

## Design

Agent-orchestra uses immutable diff identities, explicit authorization gates,
durable lifecycle states, and versioned JSON messages. See
[Design and message contract](docs/design.md) for the architecture, lifecycle,
message envelope, payloads, and validation rules.

## Scenario: uncommitted changes in a worktree

This is the primary local workflow. It covers both a worktree that already has
uncommitted changes and a worktree in which agent-orchestra first asks a
development agent to implement an objective.

1. **Enqueue the worktree.** The user or calling process runs
   `enqueue-local`, or runs `enqueue-locals` on a directory whose immediate
   children are repos. The CLI resolves `HEAD`, computes a SHA-256 identity from
   the binary tracked diff and untracked file paths and contents, creates a
   `local_changes` run in `queued`, and returns the run ID. A clean worktree is
   rejected. The command does not commit, stash, reset, or clean anything.

2. **Prepare the run.** The orchestrator moves the run to `preparing`, reads
   applicable repo instructions, records the existing worktree state, and
   confirms that the worktree and changes can be preserved. A missing or
   ambiguous worktree produces a blocked or failed result without mutation.

3. **Obtain a developer handoff.** If implementation is still required, the
   orchestrator moves the run to `developing` and sends a
   `development_assignment` containing the objective, worktree, iteration,
   timeout, and allowed actions. The developer imports
   `$agent-orchestra-developer`, edits only the assigned worktree, validates the
   result, and returns a `developer_handoff`. If the worktree was supplied with
   completed uncommitted changes, the orchestrator skips the initial developer
   invocation and treats those changes as the handoff.

4. **Freeze the review identity.** Immediately before review, the orchestrator
   recomputes the diff digest. It records the current base SHA, head SHA, and
   digest, moves the run to `awaiting_review`, and increments the review
   iteration. The digest is the approval boundary; the worktree itself is not
   assumed to remain unchanged.

5. **Send the review request.** The orchestrator sends a `review_request` with
   the objective, worktree, iteration, allowed actions, timeout, base SHA, head
   SHA, diff digest, and Markdown artifact path. The reviewer imports
   `$agent-orchestra-reviewer`, confirms the current diff still matches the
   request, and performs a read-only review. A mismatch returns `blocked`
   instead of reviewing a different change.

6. **Return and record feedback.** The reviewer returns a `review_result` with
   `approved`, `changes_requested`, or `blocked`; the reviewed digest; summary;
   ordered findings; validation evidence; and remaining verification gaps. It
   also writes the Markdown artifact to the requested path. The orchestrator
   verifies that the result names the requested run, iteration, and digest
   before accepting it.

7. **Remediate requested changes.** For `changes_requested`, the run moves to
   `changes_requested` and then `developing`. The orchestrator sends a
   `remediation_assignment` containing the original objective and complete
   review artifact. The developer evaluates every finding, makes justified
   changes, and returns a new `developer_handoff` with each finding's
   disposition. The orchestrator computes a new digest and repeats steps 4-7.
   Review iterations are bounded; exhausting the configured limit fails the
   run instead of looping forever.

8. **Handle blocked or failed work.** A blocked review remains unresolved until
   its missing scope, evidence, or decision is supplied. Agent errors,
   timeouts, or interrupted processes produce `failed` or `interrupted` with
   captured diagnostics. A resumable run continues from its last durable
   transition rather than restarting silently.

9. **Accept approval for one digest.** For `approved`, the orchestrator records
   the verdict and moves to `approved`. It recomputes the digest before any
   commit action. If the worktree changed, approval is invalidated, the run
   returns to `awaiting_review` with a new iteration, and no commit is made.

10. **Request commit authorization.** The run moves to
    `awaiting_commit_authorization`, and the orchestrator sends an
    `authorization_request` describing the exact digest and proposed commit.
    Denial cancels the action without discarding the worktree. Approval permits
    only the commit; it does not permit a push or pull request.

11. **Commit and request publication separately.** After a successful commit,
    the run moves through `committed` to
    `awaiting_publish_authorization`. A second `authorization_request` names the
    proposed push and pull-request operation. On approval, the acting agent
    publishes and returns an `operation_result` with the branch, commit, and
    pull-request identity. The run becomes `published`. Denial leaves the local
    commit intact and unpublished.

12. **Finish without destructive cleanup.** Completion reports the final run
    state and artifact locations. Worktree removal, branch deletion, merging,
    and remote cleanup are separate actions and require their own safety checks
    and authorization.

Only enqueueing, local digest capture, state storage, status inspection, and the
underlying models are implemented today. Agent invocation, review persistence,
authorization commands, commit/publication execution, iteration limits, and
resumption are the target contract described above.

## Scenario: remote pull-request review

This workflow reviews a remote PR at an exact head without changing the source
branch. It is a target design; remote provider integration and PR enqueueing are
not implemented yet.

1. **Enqueue the PR URL.** The caller submits a GitHub or GitLab pull-request
   URL. The orchestrator validates the provider and project identity, stores the
   canonical URL, creates a `pull_request` run in `queued`, and returns its run
   ID. URL parsing does not post to the provider.

2. **Resolve live PR metadata.** In `preparing`, the provider adapter reads the
   PR identifier, target branch and SHA, source branch and exact head SHA,
   current state, and relevant repository location. Authentication or lookup
   failures return a blocked or failed result with the provider diagnostic.

3. **Create an exact-head review worktree.** The orchestrator fetches the
   recorded remote head and creates or reuses an isolated detached review
   worktree according to the repo's worktree rules. It verifies that the local
   head equals the provider-reported head. It never reviews the caller's main
   worktree or checks out the contributor branch in place.

4. **Freeze the remote review scope.** The orchestrator computes the exact
   target-to-head diff and digest, records the base SHA, head SHA, and digest,
   moves to `awaiting_review`, and increments the iteration. The PR URL alone
   is never treated as an immutable review target.

5. **Send the review request.** The `review_request` contains the PR objective
   and URL, detached worktree, iteration, timeout, base SHA, head SHA, diff
   digest, and artifact path. `allowed_actions` is empty for the reviewer. The
   reviewer may run safe read-only validation but does not edit, commit, push,
   post a review, approve remotely, or merge. It imports
   `$agent-orchestra-reviewer` for this role.

6. **Detect concurrent PR updates.** Before accepting the result, the
   orchestrator reads the live PR head again and recomputes the local digest. If
   either differs from the request, the result is stale and is not published.
   The run becomes `superseded`; the new head must be enqueued as a new run and
   review scope.

7. **Return the local review result.** For an unchanged head, the reviewer
   returns `review_result` with the exact reviewed head and digest, verdict,
   structured findings, validation, and Markdown artifact. The orchestrator
   stores the result and presents it to the caller. `approved` means only that
   the reviewed diff has no actionable findings; it is not provider approval or
   merge authorization.

8. **Publish feedback only when authorized.** By default, feedback remains a
   local artifact. Posting a PR comment, submitting a provider review, or
   setting an approval state requires an `authorization_request` naming the
   exact provider action and reviewed head. If allowed, the provider adapter
   posts the structured result and returns an `operation_result` containing the
   remote review or comment identity. Merge is never implied.

9. **Handle requested changes externally.** The reviewer never fixes the PR in
   its detached worktree. The PR author or a separately authorized development
   workflow addresses findings and pushes a new head. That head invalidates the
   old review and starts another exact-head review. Feedback is carried forward
   so the next reviewer can verify every prior finding's disposition.

10. **Complete and preserve evidence.** The final result records the provider,
    PR identity, reviewed target and head SHAs, digest, verdict, artifact path,
    validation, and any remote-post identity. Removing the detached worktree is
    a separate cleanup operation performed only after preservation checks.

The shared state model will need a remote-review completion transition before
this scenario is implemented: a remote review normally stops after delivering
or posting its verdict and does not enter the local commit/publication states.

## Current scope

The initial foundation provides:

- typed run, review, and finding models;
- an explicit, validated state machine;
- SQLite run storage with transition history and optimistic updates;
- an interface for bounded external agent adapters;
- digest capture for tracked and untracked local changes;
- Markdown review rendering;
- commands to initialize state, enqueue local changes from one repo or a
  directory of repos, and inspect runs;
- a Python-native installer for Codex and Claude Code skills;
- versioned developer and reviewer skills under `skills/`.

The role skills are:

- `agent-orchestra-developer`, which implements an assigned objective or
  addresses findings in the supplied worktree while observing explicit action
  gates;
- `agent-orchestra-reviewer`, which performs a read-only review of the assigned
  immutable diff and returns structured findings and a Markdown artifact.

Both skills follow the vendor-neutral Agent Skills format and use the same
versioned `SKILL.md` in OpenAI Codex and Anthropic Claude Code. Import only the
skill relevant to an agent's assigned role so its instructions and context stay
focused.

### Developer agent skill

A development agent imports `agent-orchestra-developer`. From the root of an
agent-orchestra checkout, install it for both supported agent runtimes with:

```shell
mise run agent-orchestra -- skills install \
  --agent all \
  --skill agent-orchestra-developer
```

A development agent should invoke `$agent-orchestra-developer` for an assigned
implementation or review-remediation run. It should not import the reviewer
skill for that role.

### Reviewer agent skill

A reviewer agent imports `agent-orchestra-reviewer`. From the root of an
agent-orchestra checkout, install it for both supported agent runtimes with:

```shell
mise run agent-orchestra -- skills install \
  --agent all \
  --skill agent-orchestra-reviewer
```

A reviewer agent should invoke `$agent-orchestra-reviewer` for an assigned
immutable-diff review. It should not import the developer skill because the
reviewer role must remain read-only.

The installer uses only the Python standard library. By default it installs to
`$CODEX_HOME/skills` (or `~/.codex/skills`) for Codex and
`$CLAUDE_CONFIG_DIR/skills` (or `~/.claude/skills`) for Claude Code. Use
`--codex-home` or `--claude-home` to override those configuration roots. Each
skill installation is atomic and idempotent. A managed, unchanged installation
is upgraded when its bundled source changes; a locally modified destination is
never overwritten.

Reviews and findings are modeled and rendered but are not persisted yet.
Concrete agent execution, worktree creation, leases, Git provider integration,
review persistence, and authorization commands are intentionally left for
subsequent increments.

## Development

The project uses Python 3.14, uv, Ruff, mypy, pytest, and mise.

```shell
uv sync
mise run format
mise run lint
mise run mypy
mise run tests
```

Initialize a state database and enqueue a local repo:

```shell
mise run agent-orchestra -- init
mise run agent-orchestra -- enqueue-local /path/to/repo
mise run agent-orchestra -- status
```

Enqueue every immediate child repo with local changes. Clean repos and repos
that cannot be read are reported and skipped; the command fails only when a
repo failed and none could be enqueued:

```shell
mise run agent-orchestra -- enqueue-locals ~/Projects
```

State defaults to `.agent-orchestra/state.db`. Use `--database PATH` to choose
another location.
