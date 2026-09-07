# Agent Orchestra CLI reference

This document is the complete reference for the `agent-orchestra` command-line
interface. For the workflow model behind these commands, see
[Workflows](workflows.md). For persisted messages and output schemas, see
[Design and protocol](design.md).

## Invocation

Run the CLI from this repo through mise:

```shell
mise agent-orchestra -- COMMAND [OPTIONS]
```

After installing the distribution, invoke the entry point directly:

```shell
agent-orchestra COMMAND [OPTIONS]
```

Both forms accept the same arguments. Examples below use the installed entry
point for brevity.

Examples:

```shell
# Run from a source checkout.
mise agent-orchestra -- jobs

# Run an installed entry point.
agent-orchestra jobs
```

Example command output for an initialized database with no jobs:

```json
{
  "schema_version": 8,
  "jobs": [],
  "error": null
}
```

## Global options

```text
agent-orchestra [-h] [--version] [--database DATABASE] COMMAND
```

| Option | Default | Meaning |
|---|---|---|
| `-h`, `--help` | | Show help and exit. |
| `--version` | | Print the installed distribution version as `agent-orchestra VERSION` and exit without requiring a command. |
| `--database DATABASE` | `~/.local/state/agent-orchestra/state.db` | Select the SQLite state database. This global option must appear before the command name. |

Mutable state must remain outside any target worktree. Commands that read an
existing job do not initialize a missing database.

Examples:

```shell
# Show global help.
agent-orchestra --help

# Show the version from the repo through mise.
mise agent-orchestra -- --version

# Query a non-default state database. Global options precede the command.
agent-orchestra --database /var/tmp/orchestra/state.db jobs
```

Example output from `--version` for version `0.1.0`:

```text
agent-orchestra 0.1.0
```

`--version` and valid `--help` requests write plain text to stdout and exit 0.
Argument syntax errors write argparse usage and a diagnostic to stderr and exit
2 before a command runs.

## `init`

Initialize the configured SQLite database and its parent directory:

```text
agent-orchestra [--database DATABASE] init
```

The command is idempotent. It creates missing tables and applies supported
state-name migrations. On success it writes one plain-text line to stdout and
exits 0.

Examples:

```shell
# Initialize the default database.
agent-orchestra init

# Initialize an explicitly selected database.
agent-orchestra --database /var/tmp/orchestra/state.db init
```

Example output from the second command:

```text
initialized /var/tmp/orchestra/state.db
```

The displayed path is the selected `--database` value. Initialization does not
have a command-specific structured error envelope; an unexpected filesystem or
SQLite exception propagates as a command failure.

## `enqueue-local`

Capture the current uncommitted changes in one Git worktree:

```text
agent-orchestra [--database DATABASE] enqueue-local [--base BASE]
  [--supersedes JOB_ID] [REPOSITORY]
```

| Argument | Default | Meaning |
|---|---|---|
| `REPOSITORY` | Current directory | Git worktree, or any directory inside it, whose changes are captured. |
| `--base BASE` | `HEAD` | Git revision used as the base of the captured diff. |
| `--supersedes JOB_ID` | None | Link an exceptional replacement to a terminal `failed` or `superseded` job for the same repo and worktree. |

The command resolves the selected worktree root and its primary Git registry
entry. It stores that main location as `repository_path` and the selected
checkout as `worktree_path`. These paths are equal when the selected checkout is
the primary worktree. A linked worktree backed by a bare repository uses that
bare path. A non-bare primary worktree retains its worktree path as
`repository_path` when its Git directory is stored separately. The command then
resolves the base and current `HEAD` at the worktree root and captures the
complete worktree even when `REPOSITORY` names a subdirectory. It hashes the
binary tracked diff plus sorted untracked file paths, executable bits, symlink
targets, and contents. Ignored files are excluded. A successful enqueue prints
only the new job ID, making command substitution safe.

Examples:

```shell
# Capture the current worktree relative to HEAD.
export JOB_ID="$(agent-orchestra enqueue-local)"

# Capture a specific worktree relative to origin/main.
export JOB_ID="$(agent-orchestra enqueue-local --base origin/main /path/to/repo)"

# Replace a terminal job that cannot be resumed.
export JOB_ID="$(agent-orchestra enqueue-local \
  --supersedes 20260903T194500Z-a7f3c921 /path/to/repo)"

# Use the returned job ID in a later command.
printf '%s\n' "$JOB_ID"
```

Example output from `printf`:

```text
20260903T194500Z-a7f3c921
```

`enqueue-local` writes that value to stdout. Command substitution stores it in
`JOB_ID`; `printf` displays the stored value. Subsequent `job`, `task`, and
`run` examples consume the same variable.

Success writes the job ID to stdout and exits 0. The identifier is plain text,
not JSON. The command exits 2 when the path is not a usable Git worktree, the
revision cannot be resolved, files cannot be read, or there are no local
changes. Those expected failures write `error: MESSAGE` to stderr and nothing
to stdout. The command does not start an agent or modify the target worktree.
Use [`resume`](#resume) for `interrupted`, `validation_required`, or
conditionally recoverable `reviewing`, `developing`, `approved`, and
`changes_requested` jobs. The
`--supersedes` escape hatch accepts only terminal `failed` or `superseded`
jobs, and only when both records identify the same repo and worktree.

## `enqueue-locals`

Capture changed Git repos immediately below one directory:

```text
agent-orchestra [--database DATABASE] enqueue-locals [--base BASE] DIRECTORY
```

| Argument | Default | Meaning |
|---|---|---|
| `DIRECTORY` | Required | Parent directory whose immediate children are inspected. `~` is expanded. |
| `--base BASE` | `HEAD` | Git revision resolved independently in each repo. |

An immediate child qualifies when it is a directory containing either a `.git`
directory or a `.git` file, so linked worktrees are supported. Repos are sorted
by basename. Clean repos and non-repos are skipped.

The command writes one versioned JSON document to stdout.

Examples:

```shell
# Enqueue changed immediate children and retain the complete result.
agent-orchestra enqueue-locals ~/PersonalProjects

# Select all created job IDs for further processing.
agent-orchestra enqueue-locals ~/PersonalProjects \
  | jq -r '.jobs[].job_id'
```

Example output from the first command:

```json
{
  "schema_version": 8,
  "directory": "/Users/example/PersonalProjects",
  "jobs": [
    {
      "job_id": "20260903T194500Z-a7f3c921",
      "worktree_path": "/Users/example/PersonalProjects/agent-orchestra"
    },
    {
      "job_id": "20260903T194500Z-b8e4d032",
      "worktree_path": "/Users/example/PersonalProjects/py-fund-manager"
    }
  ],
  "summary": {
    "enqueued": 2,
    "clean": 5,
    "failed": 0
  },
  "failures": [],
  "error": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `8`. |
| `directory` | String | Resolved absolute directory that was requested. |
| `jobs` | Array | Successfully enqueued changed repos. |
| `jobs[].job_id` | String | New opaque job ID. |
| `jobs[].worktree_path` | String | Absolute path of the captured Git worktree. |
| `summary.enqueued` | Integer | Number of jobs created. |
| `summary.clean` | Integer | Number of qualifying repos skipped because they had no changes. |
| `summary.failed` | Integer | Number of qualifying repos that could not be inspected. |
| `failures` | Array | Per-repo failures that did not prevent other repos from being captured. |
| `failures[].repository_path` | String | Absolute path of the repo that failed. |
| `failures[].message` | String | Human-readable diagnostic for that repo. |
| `error` | Object or null | Command-level failure, otherwise `null`. |
| `error.code` | String | Stable machine-readable failure code; currently `directory_not_found`. |
| `error.message` | String | Human-readable command diagnostic. |

`jobs` follows repo-basename order. Each item contains the new job ID and
absolute worktree path. `summary` contains
numeric counts, and `failures` contains `repository_path` and `message` for
every repo that could not be inspected. Independent failures remain in the JSON
document even when another repo enqueues successfully.

A missing input directory returns exit status 2 with empty `jobs` and
`failures`, zero summary counts, and an `error` object whose `code` is
`directory_not_found`. Other completed scans set `error` to `null`. The command
does not write diagnostics outside the JSON document for these outcomes.

To obtain one job ID, select it explicitly instead of assigning the complete
document to `JOB_ID`:

```shell
export JOB_ID="$(agent-orchestra enqueue-locals ~/PersonalProjects | jq -r '.jobs[0].job_id')"
```

Capture is completed for every candidate before any job is persisted. One
unreadable repo does not prevent independent valid repos from enqueueing. The
command exits nonzero only when at least one repo fails and none enqueue.

## Job and task views

The public hierarchy is `job` -> `task` -> `attempt`. A job is one complete
objective and workflow, a task is one durable role assignment, and an attempt
is one process execution. Four read-only, schema-version 8 JSON views expose
that hierarchy:

```text
agent-orchestra [--database DATABASE] jobs
agent-orchestra [--database DATABASE] job JOB_ID [--runs-directory DIRECTORY]
agent-orchestra [--database DATABASE] tasks JOB_ID [--runs-directory DIRECTORY]
agent-orchestra [--database DATABASE] task TASK_ID [--runs-directory DIRECTORY]
```

`jobs` lists stored jobs newest first. `job` returns one job plus a `current`
array of non-terminal tasks; the array is empty when nothing is pending or
running. `tasks` returns complete task history. `task` derives the parent job
from the globally unique task ID and returns every attempt, including contained
stdout and stderr paths and content.

```json
{
  "schema_version": 8,
  "job": {
    "job_id": "20260907T090000Z-a7f3c921",
    "state": "reviewing",
    "current": [
      {
        "task_id": "20260907T090000Z-a7f3c921:000003-reviewer",
        "role": "reviewer",
        "attempt": 2,
        "status": "running",
        "conclusion": null
      }
    ]
  },
  "error": null
}
```

`jobs` reads only the state database and therefore has no evidence-directory
option. For `job`, `tasks`, and `task`, `--runs-directory` selects the evidence
root and defaults to `~/.local/state/agent-orchestra/runs`. Each lookup resolves
the root before use and rejects job-directory and attempt-evidence escapes.

| Job field | Type | Meaning |
|---|---|---|
| `job_id` | String | Permanent opaque job identifier. |
| `state` | String | Durable workflow state. |
| `current` | Array | Non-terminal tasks; present only in the single-job view. |
| `supersedes_job_id` | String or null | Replaced terminal job, when any. |
| Other fields | Mixed | Scenario, repo/worktree identity, immutable Git scope, iteration, remote URL, and timestamps. |

| Task or attempt field | Type | Meaning |
|---|---|---|
| `task_id` | String | Globally addressable `{job_id}:{sequence}-{role}` identifier. |
| `role` | String | `developer` or `reviewer`. |
| `status` | String | Task or attempt lifecycle status. |
| `attempt_id` | String | Public identifier for one process execution. |
| `attempt` | Integer | One-based attempt ordinal. |
| `legacy` | Boolean | Always `false` for validated attempt records. |
| `streams` | Object | Separate stdout and stderr objects with path, availability, and, in task-history views, content. |

`job` reads attempt manifests to derive `current`, but does not open stream
files. `tasks` and `task` include stream content.

Stable query error codes are `state_database_not_found`, `job_not_found`,
`invalid_task_id`, `task_not_found`, and `invalid_evidence`. Errors from a
single-job query echo `job_id`; task-addressed errors echo `task_id` and also
echo the derived `job_id` when the task identifier contains one.

This is an intentional breaking migration. The former `status` and `logs`
commands and schema-7 identifier and collection fields have no
aliases. Callers must use the four commands above and the schema-8 `job_id`,
`jobs`, and `attempt_id` fields.

The new views do not reproduce the former log-filter flags. Select a task by
its stable ID, then filter the structured document with `jq`; for example,
`agent-orchestra tasks "$JOB_ID" | jq '.tasks[] | select(.role == "reviewer")'`
replaces role filtering, and
`agent-orchestra task "$TASK_ID" | jq '.task.attempts[].streams.stdout'`
selects stdout. Iteration, runtime, attempt ID, and stream are ordinary fields
in the same documents, so callers can combine filters without another CLI
schema change.

## `run`

Consume one queued local job through the bounded review and remediation loop:

```text
agent-orchestra [--database DATABASE] run JOB_ID --objective OBJECTIVE [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--objective OBJECTIVE` | Required | Review objective and acceptance context sent to the agents. Blank objectives are rejected. |
| `--timeout SECONDS` | `1800` | Positive timeout for each reviewer invocation. |
| `--developer-timeout SECONDS` | `1800` | Positive timeout for each developer remediation invocation. |
| `--max-iterations COUNT` | `3` | Positive maximum number of review iterations. |
| `--developer-agent {codex,claude-code}` | `codex` | Built-in runtime selected for development remediation. |
| `--developer-model MODEL` | Runtime default | Optional model passed to the developer adapter. |
| `--reviewer-agent {codex,claude-code}` | `codex` | Built-in runtime selected for review. |
| `--reviewer-model MODEL` | Runtime default | Optional model passed to the reviewer adapter. |
| `--runs-directory RUNS_DIRECTORY` | `~/.local/state/agent-orchestra/runs` | External directory for messages, artifacts, invocation records, logs, and failures. |

The state database and evidence directory must remain outside the target worktree.
The command verifies the current diff digest before review, after every
read-only review, and after remediation. Approval stops at
`awaiting_commit_authorization`; this command never commits or publishes work.

Examples:

```shell
# Use the default Codex developer and reviewer adapters.
agent-orchestra run "$JOB_ID" \
  --objective "Review the queued implementation"

# Select adapters, models, and the iteration bound explicitly.
agent-orchestra run "$JOB_ID" \
  --objective "Review and remediate the queued implementation" \
  --developer-agent claude-code \
  --developer-model sonnet \
  --reviewer-agent codex \
  --reviewer-model gpt-5.6 \
  --max-iterations 4
```

When orchestration completes without a command-level failure, `run` writes one
versioned JSON document to stdout:

Example output:

```json
{
  "schema_version": 8,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": "awaiting_commit_authorization",
  "error": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `8`. |
| `job_id` | String | Permanent opaque job ID. |
| `state` | String | Resulting durable [lifecycle state](design.md#lifecycle). |
| `error` | Object or null | Command-level failure, otherwise `null`. |

The resulting state is commonly `awaiting_commit_authorization` after approval,
`changes_requested` when a human decision is needed, `validation_required`
after a recoverable blocked or failed handoff, or `interrupted` after a timeout.
Agent stdout and stderr are written to external evidence files, not mixed into
this JSON; retrieve them with [`task`](#job-and-task-views). Built-in adapters tee
the underlying Codex or Claude process output into those files as it arrives,
including output produced before a timeout or nonzero exit.

Command-level execution and protocol failures exit 2, currently write
`error: MESSAGE` as plain text to stderr, and do not write a JSON document.
When possible, the same failure is also persisted as durable job evidence.

The built-in runtime combinations are independent: Codex/Codex,
Codex/Claude Code, Claude Code/Codex, and Claude Code/Claude Code are all
supported. Invocation metadata records the agent vendor, optional requested
model, effective model identities, and adapter runtime separately. Claude Code
JSON results report every model that handled the invocation, so fallback and
helper models remain visible. The current Codex machine-readable result does
not report effective model identity; Codex records therefore use
`effective_model_status: "unavailable"` without guessing from defaults or
human-formatted output. Custom commands use the same explicit unknown state.

### Custom reviewer command

Append `-- COMMAND [ARGUMENT ...]` to replace the built-in reviewer adapter:

Example:

```shell
agent-orchestra run "$JOB_ID" \
  --objective "Review the queued implementation" \
  -- /absolute/path/to/reviewer --flag
```

Example output when the custom reviewer requests changes:

```json
{
  "schema_version": 8,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": "changes_requested",
  "error": null
}
```

Agent Orchestra appends the review request and response JSON paths to the custom
command. Built-in reviewer or developer agent/model options cannot be combined
with this form. A custom reviewer has no configured developer adapter, so a
`changes_requested` verdict stops for external remediation instead of starting
the built-in loop.

## `resume`

Continue a recoverable job from its last durable request:

```text
agent-orchestra [--database DATABASE] resume JOB_ID
  [--runs-directory RUNS_DIRECTORY]
```

| Option | Default | Meaning |
|---|---|---|
| `JOB_ID` | Required | Existing job in `interrupted`, `validation_required`, or a conditionally recoverable active or intermediate state. |
| `--runs-directory RUNS_DIRECTORY` | `~/.local/state/agent-orchestra/runs` | Evidence root originally selected for `run`. |

`resume` validates execution metadata, the complete canonical message chain,
the worktree scope, and durable task-attempt evidence before invoking an agent.
Jobs left in `reviewing` or `developing` by a crash are conditionally
recoverable: a response is revalidated without relaunching, a completed attempt
has its conclusion applied, and a pending or running attempt with uncertain
activation fails closed. An intermediate `approved` job advances to commit
authorization when its durable result and diff still match. A
`changes_requested` job resumes only when its accepted result, configured
developer, iteration limit, and durable remediation evidence make the next step
unambiguous.
It reuses an unanswered review or remediation request after an interruption
only when the prior attempt has a terminal conclusion.
After a valid developer handoff reports `blocked` or `failed`, it creates the
next remediation request and retries the developer. The job ID, message
history, review iteration, and objective remain unchanged. Each retried
invocation receives a higher attempt number and new log files.

Examples:

```shell
# Continue a job shown as recoverable by `job`.
agent-orchestra resume "$JOB_ID"

# Use the same custom evidence root supplied to run.
agent-orchestra resume "$JOB_ID" --runs-directory /var/tmp/orchestra/runs
```

Successful output is versioned JSON:

```json
{
  "schema_version": 8,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": "awaiting_commit_authorization",
  "error": null
}
```

An expected failure also remains JSON on stdout and exits 2:

```json
{
  "schema_version": 8,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": null,
  "error": {
    "code": "resume_scope_changed",
    "message": "resume scope changed since the interrupted review"
  }
}
```

Stable error codes are `state_database_not_found`, `job_not_found`,
`job_not_resumable`, `concurrent_update`, `resume_metadata_unsupported`,
`resume_scope_changed`, `resume_interrupted`, `resume_execution_failed`,
`resume_activation_uncertain`, `resume_cancelled`, and `resume_evidence_invalid`.
Historical jobs whose
`execution.json` lacks the version 2 resume context fail closed with
`resume_metadata_unsupported`; start an explicitly linked replacement with
[`enqueue-local --supersedes`](#enqueue-local) only after the old job is
terminal.

## `skills`

Manage the canonical role skills bundled with the distribution:

```text
agent-orchestra skills SUBCOMMAND
```

The only current subcommand is `install`.

Example:

```shell
# List the available skills subcommands.
agent-orchestra skills --help
```

Example output:

```text
usage: agent-orchestra skills [-h] {install} ...

positional arguments:
  {install}
    install   install skills for supported local agent runtimes

options:
  -h, --help  show this help message and exit
```

### `skills install`

Install one or more bundled skills for supported local agent runtimes:

```text
agent-orchestra skills install --skill SKILL [--skill SKILL ...] [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--skill SKILL` | Required | Skill name to install. Repeat to install multiple skills; duplicate names are collapsed. |
| `--agent {codex,claude-code,all}` | `all` | Runtime installation target. |
| `--source SOURCE` | Packaged skill data | Alternate directory containing canonical skill subdirectories. |
| `--codex-home CODEX_HOME` | `$CODEX_HOME`, otherwise `~/.codex` | Override the Codex configuration root. |
| `--claude-home CLAUDE_HOME` | `$CLAUDE_CONFIG_DIR`, otherwise `~/.claude` | Override the Claude Code configuration root. |

Examples:

```shell
# Install both bundled skills for Codex and Claude Code.
agent-orchestra skills install \
  --skill agent-orchestra-developer \
  --skill agent-orchestra-reviewer

# Install only the reviewer skill into an alternate Codex home.
agent-orchestra skills install \
  --agent codex \
  --skill agent-orchestra-reviewer \
  --codex-home /var/tmp/codex
```

An unchanged installation is reported as already installed. A changed canonical
skill updates an unchanged installed copy, but the installer refuses to
overwrite local modifications. Installation is Python-native and does not use
Node.js, npm, or `npx`.

Each requested skill-target pair produces one plain-text stdout line in
deterministic target order and then skill request order:

Example output from the first command:

```text
installed agent-orchestra-developer for codex: /home/user/.codex/skills/agent-orchestra-developer
already installed agent-orchestra-reviewer for codex: /home/user/.codex/skills/agent-orchestra-reviewer
installed agent-orchestra-developer for claude-code: /home/user/.claude/skills/agent-orchestra-developer
already installed agent-orchestra-reviewer for claude-code: /home/user/.claude/skills/agent-orchestra-reviewer
```

The line begins with `installed` when files were copied or updated and `already
installed` when the destination already matched. A complete successful request
exits 0. An unknown skill, unsafe source, locally modified destination, or file
operation failure exits 2, writes `error: MESSAGE` to stderr, and writes no
result lines to stdout. Validation covers the complete request before any
destination is changed.

## Exit status

| Status | Meaning |
|---|---|
| `0` | The command completed successfully. A workflow may still be waiting for review, remediation, or authorization; inspect its returned state. |
| `2` | Arguments, local state, Git state, evidence, runtime execution, or protocol validation prevented completion. The command-specific sections state whether the diagnostic is JSON on stdout or plain text on stderr. |

Example:

```shell
if agent-orchestra job "$JOB_ID" >job-state.json; then
  jq -r '.job.state' job-state.json
else
  exit_code=$?
  printf 'job command failed with exit code %s\n' "$exit_code" >&2
fi
```

Example output when the job is queued:

```text
queued
```

Argument parsing may use argparse's standard nonzero exit behavior for invalid
syntax. Commands never treat a successful process exit alone as authorization
to commit, push, create a pull request, post, merge, or clean up a worktree.
