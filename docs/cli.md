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
existing run do not initialize a missing database.

Show the version from the repo through mise:

```shell
mise agent-orchestra -- --version
```

For version `0.1.0`, the command prints:

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
exits 0:

```text
initialized /home/user/.local/state/agent-orchestra/state.db
```

```shell
agent-orchestra init
agent-orchestra --database /var/tmp/orchestra/state.db init
```

The displayed path is the selected `--database` value. Initialization does not
have a command-specific structured error envelope; an unexpected filesystem or
SQLite exception propagates as a command failure.

## `enqueue-local`

Capture the current uncommitted changes in one Git worktree:

```text
agent-orchestra [--database DATABASE] enqueue-local [--base BASE] [REPOSITORY]
```

| Argument | Default | Meaning |
|---|---|---|
| `REPOSITORY` | Current directory | Git worktree, or any directory inside it, whose changes are captured. |
| `--base BASE` | `HEAD` | Git revision used as the base of the captured diff. |

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
only the new [run ID](design.md#run-id-format), making command substitution safe:

```text
20260903T194500Z-a7f3c921
```

```shell
export RUN_ID="$(agent-orchestra enqueue-local /path/to/repo)"
printf '%s\n' "$RUN_ID"
```

Success writes the run ID to stdout and exits 0. The identifier is plain text,
not JSON. The command exits 2 when the path is not a usable Git worktree, the
revision cannot be resolved, files cannot be read, or there are no local
changes. Those expected failures write `error: MESSAGE` to stderr and nothing
to stdout. The command does not start an agent or modify the target worktree.

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

The command writes one versioned JSON document to stdout:

```shell
agent-orchestra enqueue-locals ~/PersonalProjects
```

```json
{
  "schema_version": 3,
  "directory": "/Users/example/PersonalProjects",
  "runs": [
    {
      "id": "20260903T194500Z-a7f3c921",
      "worktree_path": "/Users/example/PersonalProjects/agent-orchestra"
    },
    {
      "id": "20260903T194500Z-b8e4d032",
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
| `schema_version` | Integer | Version of this CLI output contract; currently `3`. |
| `directory` | String | Resolved absolute directory that was requested. |
| `runs` | Array | Successfully enqueued changed repos. |
| `runs[].id` | String | New opaque [run ID](design.md#run-id-format). |
| `runs[].worktree_path` | String | Absolute path of the captured Git worktree. |
| `summary.enqueued` | Integer | Number of runs created. |
| `summary.clean` | Integer | Number of qualifying repos skipped because they had no changes. |
| `summary.failed` | Integer | Number of qualifying repos that could not be inspected. |
| `failures` | Array | Per-repo failures that did not prevent other repos from being captured. |
| `failures[].repository_path` | String | Absolute path of the repo that failed. |
| `failures[].message` | String | Human-readable diagnostic for that repo. |
| `error` | Object or null | Command-level failure, otherwise `null`. |
| `error.code` | String | Stable machine-readable failure code; currently `directory_not_found`. |
| `error.message` | String | Human-readable command diagnostic. |

`runs` follows repo-basename order. Each item contains the new
[run ID](design.md#run-id-format) and absolute worktree path. `summary` contains
numeric counts, and `failures` contains `repository_path` and `message` for
every repo that could not be inspected. Independent failures remain in the JSON
document even when another repo enqueues successfully.

A missing input directory returns exit status 2 with empty `runs` and
`failures`, zero summary counts, and an `error` object whose `code` is
`directory_not_found`. Other completed scans set `error` to `null`. The command
does not write diagnostics outside the JSON document for these outcomes.

To obtain one run ID, select it explicitly instead of assigning the complete
document to `RUN_ID`:

```shell
export RUN_ID="$(agent-orchestra enqueue-locals ~/PersonalProjects | jq -r '.runs[0].id')"
```

Capture is completed for every candidate before any run is persisted. One
unreadable repo does not prevent independent valid repos from enqueueing. The
command exits nonzero only when at least one repo fails and none enqueue.

## `status`

Show one run or list every stored run:

```text
agent-orchestra [--database DATABASE] status [RUN_ID]
```

With a run ID, the command returns that run as the sole item in `runs`. Without
one, it returns every run from newest to oldest. A database containing no runs
returns an empty `runs` array. Successful output is deterministic, indented,
versioned JSON:

```json
{
  "schema_version": 3,
  "runs": [
    {
      "id": "20260902T150612Z-a7f3c921",
      "scenario": "local_changes",
      "repository_path": "/Users/example/PersonalProjects/example-repo",
      "worktree_path": "/Users/example/PersonalProjects/example-repo",
      "state": "queued",
      "base_sha": "0123456789abcdef0123456789abcdef01234567",
      "head_sha": "0123456789abcdef0123456789abcdef01234567",
      "diff_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "iteration": 0,
      "remote_url": null,
      "created_at": "2026-09-02T15:06:12Z",
      "updated_at": "2026-09-02T15:06:12Z"
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `3`. |
| `runs` | Array | Zero or more complete run objects. |
| `runs[].id` | String | Permanent opaque [run ID](design.md#run-id-format). |
| `runs[].scenario` | String | Workflow entry point: currently `local_changes` or `pull_request`. |
| `runs[].repository_path` | String | Absolute primary repository location reported by Git: the primary worktree for a non-bare repository, including one with a separate Git directory, or the backing bare repository path. Historical local runs may contain their selected worktree here. |
| `runs[].worktree_path` | String | Absolute path to the exact worktree captured for and assigned to the run. |
| `runs[].state` | String | Current durable [lifecycle state](design.md#lifecycle). |
| `runs[].base_sha` | String | Git commit used as the diff base. |
| `runs[].head_sha` | String | Captured Git `HEAD` commit. It may equal `base_sha` for uncommitted local changes. |
| `runs[].diff_digest` | String or null | SHA-256 identity of the exact working-tree diff, normally prefixed with `sha256:`. |
| `runs[].iteration` | Integer | Current review iteration; a newly queued run starts at `0`. |
| `runs[].remote_url` | String or null | Associated remote URL when the scenario has one. |
| `runs[].created_at` | String | UTC creation timestamp in RFC 3339 form. |
| `runs[].updated_at` | String | UTC timestamp of the latest persisted update. |

This output is CLI state, not a canonical workflow message. Consumers must
check `schema_version`, treat run IDs as opaque strings, and tolerate multiple
items when no ID is supplied. The same contract is summarized in
[Run status output](design.md#run-status-output).

`status` is read-only and does not initialize or migrate state. A missing
database returns `state database not found: PATH`; an unknown run returns
`run not found: MESSAGE`. Both are plain text on stderr, return exit status 2,
and write nothing to stdout. Successful queries write only the JSON document to
stdout and exit 0.

```shell
agent-orchestra status
agent-orchestra status "$RUN_ID"
```

## `logs`

View the separate stdout and stderr streams produced by one run:

```text
agent-orchestra [--database DATABASE] logs [OPTIONS] RUN_ID
```

| Option | Default | Meaning |
|---|---|---|
| `--iteration ITERATION` | All | Include only one review iteration. |
| `--role {developer,reviewer}` | All | Include only invocations for one role. |
| `--invocation INVOCATION` | All | Include only the exact invocation ID. |
| `--runtime RUNTIME` | All | Include only one adapter runtime, such as `codex` or `claude-code`. |
| `--stream {stdout,stderr}` | Both | Include only one output stream. |
| `--runs-directory RUNS_DIRECTORY` | `~/.local/state/agent-orchestra/runs` | Select the external evidence root used by `run`. |

The command writes one versioned JSON document to stdout. Each selected stream
is a separate entry containing its invocation identity, role, agent, runtime,
iteration, timing, process outcome, resolved path, and complete content. Empty
streams are included with an empty `content` string.

```shell
agent-orchestra logs "$RUN_ID"
agent-orchestra logs "$RUN_ID" \
  --iteration 2 --role reviewer --stream stderr
agent-orchestra logs "$RUN_ID" \
  --runtime claude-code --invocation INVOCATION_ID
```

For example, a successful reviewer invocation with stdout and an empty stderr
stream returns:

```json
{
  "schema_version": 3,
  "run_id": "20260903T194500Z-a7f3c921",
  "streams": [
    {
      "invocation_id": "550e8400-e29b-41d4-a716-446655440000",
      "role": "reviewer",
      "agent_vendor": "openai",
      "agent_model": "gpt-5.3-codex",
      "runtime": "codex",
      "iteration": 1,
      "started_at": "2026-09-03T19:45:00Z",
      "finished_at": "2026-09-03T19:46:12Z",
      "exit_code": 0,
      "timed_out": false,
      "interrupted": false,
      "stream": "stdout",
      "path": "/home/user/.local/state/agent-orchestra/runs/20260903T194500Z-a7f3c921/logs/550e8400-e29b-41d4-a716-446655440000.stdout.log",
      "content": "Review completed and the structured result was written successfully.\n",
      "legacy": false
    },
    {
      "invocation_id": "550e8400-e29b-41d4-a716-446655440000",
      "role": "reviewer",
      "agent_vendor": "openai",
      "agent_model": "gpt-5.3-codex",
      "runtime": "codex",
      "iteration": 1,
      "started_at": "2026-09-03T19:45:00Z",
      "finished_at": "2026-09-03T19:46:12Z",
      "exit_code": 0,
      "timed_out": false,
      "interrupted": false,
      "stream": "stderr",
      "path": "/home/user/.local/state/agent-orchestra/runs/20260903T194500Z-a7f3c921/logs/550e8400-e29b-41d4-a716-446655440000.stderr.log",
      "content": "",
      "legacy": false
    }
  ],
  "failures": [],
  "error": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `3`. |
| `run_id` | String | Requested opaque [run ID](design.md#run-id-format). |
| `streams` | Array | Selected, available stream entries. |
| `streams[].invocation_id` | String | Invocation record ID, or a stable filename-derived ID for legacy evidence. |
| `streams[].role` | String | `developer` or `reviewer`. |
| `streams[].agent_vendor` | String or null | Selected agent provider; unavailable for legacy evidence. |
| `streams[].agent_model` | String or null | Explicit CLI model override; `null` means the runtime selected its own default. |
| `streams[].runtime` | String or null | Adapter runtime, such as `codex` or `claude-code`; unavailable for legacy evidence. |
| `streams[].iteration` | Integer or null | Positive review iteration; unavailable for legacy evidence. |
| `streams[].started_at` | String or null | UTC invocation start timestamp; unavailable for legacy evidence. |
| `streams[].finished_at` | String or null | UTC invocation finish timestamp, when known. |
| `streams[].exit_code` | Integer or null | Process exit code, when available. |
| `streams[].timed_out` | Boolean or null | Whether the invocation exceeded its timeout; unavailable for legacy evidence. |
| `streams[].interrupted` | Boolean or null | Whether the invocation was interrupted; unavailable for legacy evidence. |
| `streams[].stream` | String | `stdout` or `stderr`. |
| `streams[].path` | String | Resolved absolute path of the evidence file. |
| `streams[].content` | String | Complete UTF-8 text with undecodable bytes replaced. |
| `streams[].legacy` | Boolean | Whether identity was derived from legacy filenames. |
| `failures` | Array | Independently missing selected streams. |
| `failures[].code` | String | `missing_log`. |
| `failures[].stream` | String | Missing `stdout` or `stderr` stream. |
| `failures[].path` | String | Expected resolved evidence path. |
| `failures[].message` | String | Human-readable missing-file diagnostic. |
| `error` | Object or null | Command-level failure, otherwise `null`. |
| `error.code` | String | Stable machine-readable failure code. |
| `error.message` | String | Human-readable command diagnostic. |

`streams` is ordered by invocation-record filename and then stdout before
stderr. Selecting `--stream stderr` returns only matching stderr entries.
`exit_code`, `finished_at`, and the Boolean outcome fields can be `null` when
the process outcome is unavailable. Legacy filename-only entries set `legacy`
to `true` and unavailable agent, runtime, iteration, timing, and outcome fields
to `null`.

The command is read-only and never uploads logs. It rejects evidence traversal,
symlink escape, malformed invocation records, mismatched run IDs, and paths
outside the configured runs directory. A missing stream leaves available
entries in `streams`, adds a `missing_log` object to `failures`, and exits 2.
Command-level failures leave `streams` and `failures` empty, put a stable code
and message in `error`, and exit 2. Stable error codes are
`state_database_not_found`, `run_not_found`, `run_evidence_escape`,
`run_evidence_not_found`, `legacy_metadata_unavailable`, `invalid_evidence`,
and `no_matching_logs`. Expected outcomes, including failures, are JSON on
stdout; stderr remains empty. Filters that depend on unavailable legacy
metadata are rejected.

Process output may contain sensitive information. Treat displayed and copied
logs accordingly.

## `run`

Consume one queued local run through the bounded review and remediation loop:

```text
agent-orchestra [--database DATABASE] run RUN_ID --objective OBJECTIVE [OPTIONS]
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

The state database and runs directory must remain outside the target worktree.
The command verifies the current diff digest before review, after every
read-only review, and after remediation. Approval stops at
`awaiting_commit_authorization`; this command never commits or publishes work.

```shell
agent-orchestra run "$RUN_ID" \
  --objective "Review the queued implementation"

agent-orchestra run "$RUN_ID" \
  --objective "Review and remediate the queued implementation" \
  --developer-agent claude-code \
  --developer-model sonnet \
  --reviewer-agent codex \
  --reviewer-model MODEL \
  --max-iterations 4
```

When orchestration completes without a command-level failure, `run` writes one
versioned JSON document to stdout:

```json
{
  "schema_version": 3,
  "run_id": "20260903T194500Z-a7f3c921",
  "state": "awaiting_commit_authorization"
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `3`. |
| `run_id` | String | Permanent opaque [run ID](design.md#run-id-format). |
| `state` | String | Resulting durable [lifecycle state](design.md#lifecycle). |

The resulting state is commonly `awaiting_commit_authorization` after approval
or `changes_requested` when work must stop for external remediation or a human
decision. Agent stdout and stderr are written to external evidence files, not
mixed into this JSON; retrieve them with [`logs`](#logs). Built-in adapters tee
the underlying Codex or Claude process output into those files as it arrives,
including output produced before a timeout or nonzero exit.

Command-level execution and protocol failures exit 2, currently write
`error: MESSAGE` as plain text to stderr, and do not write a JSON document.
When possible, the same failure is also persisted as durable run evidence.

The built-in runtime combinations are independent: Codex/Codex,
Codex/Claude Code, Claude Code/Codex, and Claude Code/Claude Code are all
supported. Invocation metadata records the agent vendor and any explicit model
override separately from the adapter runtime. `agent_model` is `null` when no
override was supplied because the orchestrator cannot verify which effective
model the runtime or remote service selected.

### Custom reviewer command

Append `-- COMMAND [ARGUMENT ...]` to replace the built-in reviewer adapter:

```shell
agent-orchestra run "$RUN_ID" \
  --objective "Review the queued implementation" \
  -- /absolute/path/to/reviewer --flag
```

Agent Orchestra appends the review request and response JSON paths to the custom
command. Built-in reviewer or developer agent/model options cannot be combined
with this form. A custom reviewer has no configured developer adapter, so a
`changes_requested` verdict stops for external remediation instead of starting
the built-in loop.

## `skills`

Manage the canonical role skills bundled with the distribution:

```text
agent-orchestra skills SUBCOMMAND
```

The only current subcommand is `install`.

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

```shell
agent-orchestra skills install \
  --skill agent-orchestra-developer \
  --skill agent-orchestra-reviewer

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

Argument parsing may use argparse's standard nonzero exit behavior for invalid
syntax. Commands never treat a successful process exit alone as authorization
to commit, push, create a pull request, post, merge, or clean up a worktree.
