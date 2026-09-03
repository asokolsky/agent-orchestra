# agent-orchestra

`agent-orchestra` is a local CLI for coordinating coding agents. Each agent gets
a role and an assigned Git worktree. Workflow state and review artifacts stay
outside that worktree.

Use an isolated linked worktree for development and an exact-head detached
worktree for remote review.

## Problem We Are Trying to Solve

Coding agents can implement and review changes, but coordinating several agent
invocations is still largely manual.

Agent-orchestra intends to be a thin coordination layer offering improved agent productivity.

## Concepts

See [Roles, runtimes, adapters, and capabilities](docs/concepts.md) for the
canonical definitions.

## Supported scenarios

- In the [local development and review workflow](docs/workflows.md#local-development-and-review),
  before a development agent commits its work, a reviewer agent reviews the
  diff and hands structured feedback back to the developer. The cycle repeats
  until the review is approved, after which the workflow may request permission
  to commit and open a pull request.
- The [remote pull-request review workflow](docs/workflows.md#remote-pull-request-review)
  begins from a pull-request URL and reviews one exact remote head.

The local cycle is partially implemented. Remote pull-request enqueueing is not
implemented yet. The [design and message contract](docs/design.md) defines the
shared protocol.

## Development and review cycle

Start with any local repo.

### 1. Install the role skills

Install both bundled skills for Codex and Claude Code:

```shell
mise agent-orchestra -- skills install \
  --skill agent-orchestra-developer --skill agent-orchestra-reviewer
```

By default, the command installs the selected skills for both Codex and Claude
Code. Use `--agent codex` or `--agent claude-code` only to target one runtime.

Repeat installation after a skill version changes. The installer updates an
unchanged installation but does not overwrite local edits. It uses only the
Python standard library and installs to
`$CODEX_HOME/skills` (or `~/.codex/skills`) and
`$CLAUDE_CONFIG_DIR/skills` (or `~/.claude/skills`) by default. Use
`--codex-home` or `--claude-home` to override those roots.

### 2. Develop the change

For new work, create or reuse an isolated linked worktree according to the
repo's policy. Start a coding agent there and give it a development assignment.

Invoke `$agent-orchestra-developer` in Codex or
`/agent-orchestra-developer` in Claude Code. The template uses a generic
placeholder.

```text
<invoke the agent-orchestra-developer skill>

Objective: <describe the requested change and acceptance criteria>.

Work only in the current worktree.
Allowed actions: edit files and run local validation.
Leave validated changes uncommitted and return a ready_for_review handoff.
```

Name additional allowed actions only when they are intended. Editing does not
authorize a commit, and a commit does not authorize a push or pull request.

### 3. Review the diff

After the developer returns a handoff, start a new agent in the same worktree.
Invoke `$agent-orchestra-reviewer` in Codex or
`/agent-orchestra-reviewer` in Claude Code:

```text
<invoke the agent-orchestra-reviewer skill>

Review the exact uncommitted diff in the current worktree against <base SHA>.
Objective: <repeat the original objective and acceptance criteria>.
Do not modify the worktree or perform remote actions.
Write the review to <absolute-review-artifact-path>.
```

Keep workflow review artifacts outside the target worktree. For another review,
provide both the prior artifact and the exact current diff.

### 4. Address review findings

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

### 5. Authorize each Git action

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

## Capture and review existing changes

Use the implemented CLI path when a worktree already contains the change to
review.

### 1. Capture the diff

Run `enqueue-local` to record one repo's uncommitted diff. Use
`enqueue-locals` to scan immediate child repos, record dirty ones, and skip
clean ones. Neither command starts development or review:

```shell
export RUN_ID="$(mise agent-orchestra -- enqueue-local /path/to/dirty/repo)"
mise agent-orchestra -- status
```

`enqueue-local` prints only the new run ID, so command substitution can save it
directly in `RUN_ID`. Confirm it before continuing:

```shell
printf '%s\n' "$RUN_ID"
```

`enqueue-locals` prints one line per dirty repo with the run ID followed by its
worktree path. If the enqueue output is no longer available, run `status` and
use the `id` from the matching repo entry. See
[Run status output](docs/design.md#run-status-output) for the formatted JSON
contract and example.

### 2. Run the review

Start the review with the built-in Codex adapter:

```shell
mise agent-orchestra -- run "$RUN_ID" \
  --objective "Review the queued implementation"
```

The adapter runs `codex exec` non-interactively in a read-only sandbox. It
ignores personal Codex settings but keeps authentication and installed skills.
It writes the validated response and Markdown review outside the target
worktree. To use another adapter, add `--` and its command; agent-orchestra
appends the request and response JSON paths.

If the managed Codex default is not supported by the locally installed CLI,
select a compatible model explicitly with `--codex-model <model>`.

Keep the state database and `--runs-directory` outside the reviewed worktree.
The worker stores the request, response, review, and logs there without changing
the review digest.

### 3. Inspect the result

Run `status` and inspect the stored response, review, and logs. Approval stops
at `awaiting_commit_authorization`; requested changes stop at
`changes_requested`. Return requested changes to a developer, or authorize the
next Git action separately after approval.

The [workflow contracts](docs/workflows.md) describe the intended automatic
orchestration.

## Current scope

The current implementation provides:

- typed run, review, and finding models;
- an explicit, validated state machine;
- SQLite run storage with transition history and optimistic updates;
- an interface for agent adapters with timeouts;
- digest capture for tracked and untracked local changes;
- Markdown review rendering;
- commands to initialize state, enqueue local changes from one repo or a
  directory of repos, and inspect runs;
- a Python-native installer for Codex and Claude Code skills;
- versioned developer and reviewer skills under `skills/`;
- a built-in Codex adapter for the reviewer role and a custom reviewer-command
  escape hatch.

The supported roles are documented separately:

- [Developer role](docs/role-developer.md)
- [Reviewer role](docs/role-reviewer.md)

Installation and invocation examples are in
[Development and review cycle](#development-and-review-cycle).

One local review request, result, artifact, and its process logs are persisted
today. Developer dispatch, automated remediation, Claude Code adapters, generic
role registration, worktree creation, leases, Git provider integration, and
authorization commands remain subsequent increments.

## Development

The project uses Python 3.14, uv, Ruff, mypy, pytest, and mise.

```shell
uv sync
mise run format
mise run lint
mise run mypy
mise run tests
mise run build
git diff --check
```
