# agent-orchestra

Agents are good.  Collaborating agents are even better.
Claude has
[sub-agents](https://code.claude.com/docs/en/sub-agents)
and OpenAI has
[subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
But... How about combining the agents from different vendors?

[`agent-orchestra`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md) is a local CLI for coordinating coding agents.
Each agent gets a role and an assigned Git worktree. Workflow state and review
artifacts stay outside that worktree.

What do you get after the agents finish? An approval tied to one exact diff,
schema-validated responses, and evidence you can inspect later.

Use an isolated linked worktree for development and an exact-head detached
worktree for remote review.

## Problem We Are Trying to Solve

Coding agents can implement and review changes, but coordinating several agent
task attempts is still largely manual.

Agent-orchestra intends to be a thin coordination layer offering improved agent productivity.

## Concepts

See [Roles, runtimes, adapters, and capabilities](https://github.com/asokolsky/agent-orchestra/blob/main/docs/concepts.md) for the
canonical definitions. See the [CLI reference](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md) for every command,
option, default, output, and exit behavior.

Agent Orchestra distinguishes source-code reviewers and source-code developers,
which exchange diff-bound findings, from issue reviewers and issue creators,
which exchange readiness feedback about issue prose. The shorter persisted role
values `reviewer` and `developer` refer to the source-code roles unless an
`issue_review` job supplies the scenario context.

## Toolchain

The project targets Python 3.14 and requires Git 2.36 or newer for
NUL-delimited worktree metadata. `mise` installs `uv` and provides the routine
project tasks. `uv` manages the virtual environment and dependencies, runs the
Python tools, and builds the source and wheel distributions. Ruff provides
formatting and linting, mypy checks types, and pytest runs the test suite.

## Installation

Agent Orchestra currently requires Python 3.14. Install the published CLI with
[`uv`](https://docs.astral.sh/uv/guides/tools/):

```shell
uv tool install py-agent-orchestra
agent-orchestra --version
```

[`pipx`](https://pipx.pypa.io/) is an equivalent option:

```shell
pipx install py-agent-orchestra
```

The PyPI project is named `py-agent-orchestra`; it installs the
`agent-orchestra` command. Then install the bundled developer and reviewer
skills for Codex and Claude Code:

```shell
agent-orchestra skills install \
  --skill agent-orchestra-developer \
  --skill agent-orchestra-reviewer
```

The [primer](https://github.com/asokolsky/agent-orchestra/blob/main/docs/primer.md) continues from here. If you want to change Agent
Orchestra itself, use the source-checkout steps under [Development](#development).

Provider diagnostics, built-in runtime arguments, and canonical evidence names
are declared in versioned TOML files under
`src/agent_orchestra/manifest/`. These files ship in both distribution formats
and are validated before the CLI handles a command. See
[Packaged knowledge manifests](https://github.com/asokolsky/agent-orchestra/blob/main/docs/design.md#packaged-knowledge-manifests) for
the schema, compatibility rules, and stable failure codes.

## Supported scenarios

- The implemented [local development and review workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#local-development-and-review)
  captures an existing uncommitted diff as a job, dispatches an independent
  source-code reviewer, sends structured findings to a source-code developer
  for remediation, and
  repeats review against each new diff digest. Codex and Claude Code can be
  selected independently for either role. Interrupted and validation-required
  jobs can resume from durable task and attempt evidence. Approval stops at the
  commit-authorization boundary; committing and publishing remain separate
  user-authorized actions.
- The implemented [issue-refinement workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#issue-refinement)
  captures a GitHub or GitLab issue and reviews its immutable source digest
  before development
  begins. An issue reviewer checks that its problem statement, scope, constraints,
  risks, and acceptance criteria are clear and testable, then communicates
  actionable feedback to the issue creator. The issue can be revised and reviewed
  again until it is ready for implementation. Codex and Claude Code receive the
  same provider-neutral request. Review is read-only; the generated feedback
  can be posted only through a separate explicitly authorized command.
- The implemented [`audit`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#audit) view reconstructs ordered state,
  tasks, attempts, canonical message summaries, and provider actions for either
  workflow. Optional local verification checks the finalized evidence index and
  hashes without reading process-stream contents or contacting a provider.
- The implemented [`stats`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#stats) report summarizes review verdicts,
  job standing, and finding dispositions across a rolling time window while
  identifying jobs whose history is unavailable.
- The implemented [settings and retention commands](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#global-settings)
  provide XDG-aware storage defaults, effective-value inspection, and a
  dry-run-first policy for expiring terminal job evidence. Database cleanup and
  unmatched-directory cleanup require separate explicit options.
- The designed [remote pull-request review workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#remote-pull-request-review)
  starts from a pull-request URL and reviews one exact remote head. Remote
  pull-request enqueueing and provider-side review actions are not implemented.

The [design and message contract](https://github.com/asokolsky/agent-orchestra/blob/main/docs/design.md) defines the shared protocol
and the [CLI reference](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md) documents the implemented commands.

## Using it

The [primer](https://github.com/asokolsky/agent-orchestra/blob/main/docs/primer.md) takes you from an unreviewed change to a review you
can act on: installing the role skills, capturing a diff, running the review, and
reading the result.

The [documentation index](https://github.com/asokolsky/agent-orchestra/blob/main/docs/README.md) says what every other document is for.

## Current scope

The current implementation provides:

- typed job, review, and finding models;
- an explicit, validated state machine;
- SQLite job storage with transition history and optimistic updates;
- an interface for agent adapters with timeouts;
- digest capture for tracked and untracked local changes;
- Markdown review rendering;
- commands to initialize state, enqueue local changes from one repo or a
  directory of repos, and inspect jobs and tasks;
- commands to capture GitHub and GitLab issues and run digest-bound,
  provider-neutral readiness reviews;
- XDG-aware persistent settings plus dry-run-first evidence retention with
  auditable expiry markers and fail-closed orphan handling;
- a Python-native installer for Codex and Claude Code skills;
- versioned developer and reviewer skills under `skills/`;
- built-in Codex and Claude Code adapters for developer and reviewer roles,
  selected independently, plus a custom one-review command escape hatch;
- a bounded remediation loop with strict messages, finding dispositions,
  digest progress checks, role-specific timeouts, resumable interruptions and
  blocked handoffs, and iteration exhaustion;
- adapter-neutral attempt records separating requested and effective model
  provenance, plus read-only process stream viewing through tasks.

The source-code roles are documented separately:

- [Source-code developer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-developer.md)
- [Source-code reviewer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-reviewer.md)
- [Issue reviewer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-issue-reviewer.md)
- [Issue creator responsibility](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-issue-creator.md)

Installation and invocation examples are in the
[primer](https://github.com/asokolsky/agent-orchestra/blob/main/docs/primer.md).

Every review and remediation request, result, artifact, attempt
configuration, process log, and terminal failure is persisted outside the
worktree. Recoverable jobs continue with the same job ID through the
[`resume` command](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#resume); terminal replacements can retain lineage
through [`enqueue-local --supersedes`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#enqueue-local). Initial
clean-worktree development, worktree creation, leases, and remote pull-request
operations remain subsequent increments. Issue-review feedback can be posted to
GitHub or GitLab only through the explicit `post-issue-feedback --authorize`
boundary.

## Development

Install the toolchain, synchronize the development dependencies, and run the
standard gates:

```shell
mise install
uv sync --group dev
mise run format
mise run lint
mise run mypy
mise run tests
mise run build
mise run verify-dist
git diff --check
```

Continuous integration runs the same gates on every push and pull request,
substituting `mise run format-check` for `mise run format` so a branch is
verified rather than rewritten. Run `mise run format-check` locally to see what
CI will see.

Authenticated runtime checks are deliberately separate from the ordinary test
suite. After installing the corresponding role skills, run
`mise run test-live-claude` or `mise run test-live-codex`; see
[Opt-in live runtime verification](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#opt-in-live-runtime-verification)
for prerequisites, cost, isolation, and failure semantics.

`mise run tests` distributes the suite across one worker per available CPU,
which takes it from about a minute to about fifteen seconds. Parallel workers
interleave their output, so use `mise run tests-serial` when reading a single
failure: it runs everything in one verbose process. Both tasks run the same
tests and must both pass.
