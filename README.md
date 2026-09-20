# agent-orchestra

Agents are good.  Collaborating agents are better.
Claude has [sub-agents](https://code.claude.com/docs/en/sub-agents)
and OpenAI has
[subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
But... How about combining the agents from different vendors?

[`agent-orchestra`](https://github.com/asokolsky/agent-orchestra/)
is a local CLI that coordinates coding agents from different vendors.
For source-code workflows, each agent has a role and operates inside a
[Git worktree](https://git-scm.com/docs/git-worktree) you select.

What do you get after a source-code review finishes successfully? An approval
tied to one exact diff, schema-validated responses, and evidence you can inspect
later.

## Concepts

See [Roles, runtimes, adapters, and capabilities](https://github.com/asokolsky/agent-orchestra/blob/main/docs/concepts.md)
for the canonical definitions. See the
[CLI reference](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md)
for every command, option, default, output, and exit behavior.

## Prerequisites

[Install](https://docs.astral.sh/uv/getting-started/installation/) or
[upgrade](https://docs.astral.sh/uv/getting-started/installation/#upgrading-uv)
`uv`: each uv release carries a fixed list of downloadable Python builds.

You should also have:

- Git 2.36 or newer and
- at least one supported agent runtime, Codex or Claude Code, installed,
  configured, and authenticated.

## Installation

The PyPI project is named `py-agent-orchestra`.  It packages the
`agent-orchestra` command into its own persistent virtual environment.
Install it with [`uv`](https://docs.astral.sh/uv/guides/tools/):

```shell
uv tool install --python 3.14 py-agent-orchestra
agent-orchestra --version
```

Next install the bundled developer and reviewer skills for Codex and Claude
Code:

```shell
agent-orchestra skills install \
  --skill agent-orchestra-developer --skill agent-orchestra-reviewer
```

The [primer](https://github.com/asokolsky/agent-orchestra/blob/main/docs/primer.md)
continues from here.

## Supported scenarios

- The [local development and review workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#local-development-and-review)
  captures an existing uncommitted diff as a job, dispatches an independent
  source-code reviewer, sends structured findings to a source-code developer
  for remediation, and
  repeats review against each new diff digest. Codex and Claude Code can be
  selected independently for either role. Interrupted and validation-required
  jobs can resume from durable task and attempt evidence. Approval stops at the
  commit-authorization boundary; committing and publishing remain separate
  user-authorized actions.
- The [issue-refinement workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#issue-refinement)
  captures a GitHub or GitLab issue and reviews its immutable source digest
  before development begins. An issue reviewer checks that its problem
  statement, scope, constraints, risks, and acceptance criteria are clear and
  testable, then communicates actionable feedback to the issue creator. The
  issue can be revised and reviewed again until it is ready for implementation.
  Codex and Claude Code receive the same provider-neutral request. Review is
  read-only; the generated feedback can be posted only through a separate
  explicitly authorized command.
- The [`audit`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#audit)
  view reconstructs ordered state, tasks, attempts, canonical message summaries,
  and provider actions for either workflow. Optional local verification checks
  the finalized evidence index and hashes without reading process-stream
  contents or contacting a provider.
- The [`stats`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#stats)
  report summarizes review verdicts, job standing, and finding dispositions
  across a rolling time window while identifying jobs whose history is
  unavailable.
- The [settings and retention commands](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#global-settings)
  provide XDG-aware storage defaults, effective-value inspection, and a
  dry-run-first policy for expiring terminal job evidence. Database cleanup and
  unmatched-directory cleanup require separate explicit options.
- The designed [remote pull-request review workflow](https://github.com/asokolsky/agent-orchestra/blob/main/docs/workflows.md#remote-pull-request-review)
  starts from a pull-request URL and reviews one exact remote head. Remote
  pull-request enqueueing and provider-side review actions are not implemented.

The [design and message contract](https://github.com/asokolsky/agent-orchestra/blob/main/docs/design.md)
defines the shared protocol and the
[CLI reference](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md)
documents the implemented commands.

## Using it

The [primer](https://github.com/asokolsky/agent-orchestra/blob/main/docs/primer.md)
takes you from an unreviewed change to a review you can act on: installing the
role skills, capturing a diff, running the review, and reading the result.

The [documentation index](https://github.com/asokolsky/agent-orchestra/blob/main/docs/README.md) says what every other document is for.

## Current scope

The current implementation provides:

- validated, SQLite-backed state for local-diff and issue-refinement jobs;
- exact-digest evidence, Markdown review rendering, and resumable review and
  remediation;
- built-in Codex and Claude Code adapters plus versioned developer and reviewer
  skills;
- adapter-neutral attempt records with requested and effective model
  provenance;
- audit, statistics, settings, and dry-run-first evidence-retention commands.

The source-code roles are documented separately:

- [Source-code developer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-developer.md)
- [Source-code reviewer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-reviewer.md)
- [Issue reviewer role](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-issue-reviewer.md)
- [Issue creator responsibility](https://github.com/asokolsky/agent-orchestra/blob/main/docs/role-issue-creator.md)

Every review and remediation request, result, artifact, attempt
configuration, process log, and terminal failure is persisted outside the
worktree. Recoverable jobs continue with the same job ID through the
[`resume` command](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#resume); terminal replacements can retain lineage
through [`enqueue-local --supersedes`](https://github.com/asokolsky/agent-orchestra/blob/main/docs/cli.md#enqueue-local).

## Toolchain

We use:

- Git 2.36 or newer for NUL-delimited worktree metadata.
- Python 3.14
- `uv` to manage the virtual environment and dependencies, to run the
  tools, e.g. to build the source and wheel distributions.
- `mise` to execute the routine project tasks.
- `ruff` to format and lint sources.
- `mypy` to check types.

## Development

[Install](https://mise.jdx.dev/getting-started.html) or
[update](https://mise.jdx.dev/cli/self-update.html) `mise`.

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

## Testing

See the
[testing guide](https://github.com/asokolsky/agent-orchestra/blob/main/tests/README.md)
for the test layout, offline and serial commands, and opt-in live runtime checks.

## CI/CD

The [CI workflow](https://github.com/asokolsky/agent-orchestra/actions/workflows/ci.yml)
runs the same gates on every push and pull request. Run `mise run format-check`
locally to see what CI will see.

Publishing a GitHub release starts the
[release workflow](https://github.com/asokolsky/agent-orchestra/actions/workflows/release.yml).
The
[release process](https://github.com/asokolsky/agent-orchestra/blob/main/docs/releasing.md)
documents preparation, publication, and post-release verification.
