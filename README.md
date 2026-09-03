# agent-orchestra

[`agent-orchestra`](docs/cli.md) is a local CLI for coordinating coding agents.
Each agent gets a role and an assigned Git worktree. Workflow state and review
artifacts stay outside that worktree.

Use an isolated linked worktree for development and an exact-head detached
worktree for remote review.

## Problem We Are Trying to Solve

Coding agents can implement and review changes, but coordinating several agent
invocations is still largely manual.

Agent-orchestra intends to be a thin coordination layer offering improved agent productivity.

## Concepts

See [Roles, runtimes, adapters, and capabilities](docs/concepts.md) for the
canonical definitions. See the [CLI reference](docs/cli.md) for every command,
option, default, output, and exit behavior.

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

Install both bundled skills for Codex and Claude Code with
[`skills install`](docs/cli.md#skills-install):

```shell
mise agent-orchestra -- skills install \
  --skill agent-orchestra-developer --skill agent-orchestra-reviewer
```

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

Use the implemented [CLI path](docs/cli.md) when a worktree already contains
the change to review.

### 1. Capture the diff

Run [`enqueue-local`](docs/cli.md#enqueue-local) to record one repo's
uncommitted diff. Use [`enqueue-locals`](docs/cli.md#enqueue-locals) to scan
immediate child repos, record dirty ones, and skip clean ones. Neither command
starts development or review. The second example uses
[`status`](docs/cli.md#status):

```shell
export RUN_ID="$(mise agent-orchestra -- enqueue-local /path/to/dirty/repo)"
mise agent-orchestra -- status
```

[`enqueue-local`](docs/cli.md#enqueue-local) prints only the new run ID, so
command substitution can save it directly in `RUN_ID`. Confirm it before
continuing:

```shell
printf '%s\n' "$RUN_ID"
```

[`enqueue-locals`](docs/cli.md#enqueue-locals) prints one versioned JSON
document containing the enqueued runs, summary counts, and per-repo failures.
Select a run from its `runs` array. If the enqueue output is no longer
available, run [`status`](docs/cli.md#status) and use the `id` from the matching
repo entry. See the [batch CLI output contract](docs/cli.md#enqueue-locals) and
[Run status output](docs/design.md#run-status-output) for recovery through
[`status`](docs/cli.md#status).

### 2. Run the review

Start the review with the default built-in Codex adapter through
[`run`](docs/cli.md#run):

```shell
mise agent-orchestra -- run "$RUN_ID" \
  --objective "Review the queued implementation"
```

The adapter runs `codex exec` non-interactively in a read-only sandbox. It
ignores personal Codex settings but keeps authentication and installed skills.
It writes the validated response and Markdown review outside the target
worktree. To use another adapter, add `--` and its command; the
[`run` command](docs/cli.md#custom-reviewer-command) appends the request and
response JSON paths.

If the managed Codex default is not supported by the locally installed
[CLI](docs/cli.md#run), select a compatible model explicitly with
`--reviewer-model <model>`.

Select Claude Code independently for the reviewer role with
[`run`](docs/cli.md#run):

```shell
mise agent-orchestra -- run "$RUN_ID" \
  --objective "Review the queued implementation" \
  --reviewer-agent claude-code \
  --reviewer-model sonnet
```

Select the developer runtime and its model independently with
`--developer-agent` and `--developer-model`. The default developer and reviewer
are both Codex. `--timeout` bounds each review, `--developer-timeout` bounds
each remediation, and `--max-iterations` limits review requests (default: 3).
The Claude Code developer runs with OS-enforced filesystem sandboxing: writes
use the runtime's primary-working-directory boundary rooted at the assigned
worktree, user and project setting sources are excluded, unsandboxed retries
are disabled, and a missing sandbox is a hard failure.

Both built-in adapters invoke the same canonical reviewer skill, receive the
same request, and return the same validated result schema. Runtime output is
teed into durable stdout and stderr logs as it arrives and does not enter the
durable message contract. Output written before a timeout or nonzero exit
remains inspectable.
The Claude Code reviewer also excludes personal settings and MCP servers and
runs shell inspection under an OS-enforced read-only worktree sandbox.

Keep the state database and
[`--runs-directory`](docs/cli.md#run) outside the reviewed worktree. The worker
stores the request, response, review, and logs there without changing the review
digest.

### 3. Inspect the result

Run [`status`](docs/cli.md#status) to inspect durable workflow state. Use
[`logs`](docs/cli.md#logs) to receive a versioned JSON document containing
separate stdout and stderr entries with their role, agent, runtime, iteration,
timing, and process outcome:

```shell
mise agent-orchestra -- logs "$RUN_ID"
mise agent-orchestra -- logs "$RUN_ID" \
  --iteration 2 --role reviewer --stream stderr
```

The [`logs` command](docs/cli.md#logs) also filters by `--invocation` and
`--runtime`. Pass the same `--runs-directory` used by
[`run`](docs/cli.md#run) when using a non-default evidence root. It is read-only,
never uploads process output, and reports unsafe, malformed, or missing evidence
through the same JSON contract instead of following paths outside the selected
run. Older filename-only logs remain viewable with unavailable identity fields
set to `null`.

A built-in review that requests changes dispatches the
selected developer and repeats review after a new digest is produced. Approval
stops at `awaiting_commit_authorization`; blocked review, non-progress, invalid
handoff, timeout, or iteration exhaustion stops without committing. A custom
reviewer command retains the one-review compatibility path and stops at
`changes_requested` because no custom developer command is configured.
Worker failures are also recorded in the run's `failure.json`, so the exact
stable diagnostic remains available after the [CLI](docs/cli.md#run) exits.
When the developer rejects or blocks every finding with rationale and makes no
change, the run returns to `changes_requested` and records
`decision-required.json` for human resolution instead of misclassifying the
disagreement as non-progress.

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
- built-in Codex and Claude Code adapters for developer and reviewer roles,
  selected independently, plus a custom one-review command escape hatch;
- a bounded remediation loop with strict messages, finding dispositions,
  digest progress checks, role-specific timeouts, and iteration exhaustion.
- adapter-neutral invocation records and read-only, filterable process log
  viewing with legacy-log support.

The supported roles are documented separately:

- [Developer role](docs/role-developer.md)
- [Reviewer role](docs/role-reviewer.md)

Installation and invocation examples are in
[Development and review cycle](#development-and-review-cycle).

Every review and remediation request, result, artifact, invocation
configuration, process log, and terminal failure is persisted outside the
worktree. Initial clean-worktree development, worktree creation, leases, Git
provider integration, and authorization commands remain subsequent increments.

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
