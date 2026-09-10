# Agent Orchestra CLI reference

This document is the complete reference for the `agent-orchestra` command-line
interface. For the workflow model behind these commands, see
[Workflows](workflows.md). For persisted messages and output schemas, see
[Design and protocol](design.md).

In this reference, source-code reviewers inspect immutable diffs and
source-code developers edit worktrees. Issue reviewers inspect immutable issue
snapshots, while issue creators revise issue prose in response. Commands retain
the shorter option names `--reviewer-agent` and `--developer-agent`.

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

Before parsing or running a command, the CLI validates its packaged provider,
runtime-adapter, and evidence-name manifests. An invalid manifest exits 2 with
a schema-versioned JSON error whose code is `manifest_malformed`,
`manifest_schema_version_unsupported`, or `manifest_engine_too_old`. These failures
indicate an invalid or incompatible installation; reinstall or upgrade
`agent-orchestra` rather than editing installed package data. Manifests are part
of the installation and have no override mechanism, because runtime profiles
carry the agent capability ceiling; see
[Why manifests are packaged rather than configurable](design.md#why-manifests-are-packaged-rather-than-configurable).
The manifest schema and compatibility contract are documented in
[Design and protocol](design.md#packaged-knowledge-manifests).

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
  "schema_version": 18,
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
  "schema_version": 18,
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
| `schema_version` | Integer | Version of this CLI output contract; currently `18`. |
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

## `enqueue-issue`

Capture one GitHub or GitLab issue as an immutable issue-review job:

```text
agent-orchestra [--database DATABASE] enqueue-issue ISSUE_URL
  [--runs-directory DIRECTORY]
```

The URL must use HTTPS and canonical provider syntax:

```text
https://github.com/OWNER/REPOSITORY/issues/NUMBER
https://gitlab.com/NAMESPACE/PROJECT/-/issues/IID
https://gitlab.example.test/NAMESPACE/PROJECT/-/issues/IID
```

GitLab namespaces may be nested. GitHub is read through authenticated `gh api`;
GitLab is read through `glab api --hostname HOST`, including configured
self-managed hosts and private projects visible to the current credentials. A
self-managed host is configured when `glab auth status --hostname HOST`
succeeds; otherwise the command reports `gitlab_host_not_configured` and asks
the user to run `glab auth login --hostname HOST`. URL parsing itself performs
no process or network access.

The command normalizes provider fields, computes a deterministic digest over
title, body, labels, and state, writes `issue.json` beneath the selected
evidence root's UTC date shard, persists the queued job, prints its opaque ID,
and exits 0. It does not run an agent or write to the provider. Invalid URLs,
unavailable provider CLIs, authentication failures, missing issues, malformed
responses, and evidence-path violations write a diagnostic to stderr and exit
2.

## `review-issue`

Review a captured issue for implementation readiness:

```text
agent-orchestra [--database DATABASE] review-issue JOB_ID [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--objective TEXT` | `Review this issue for implementation readiness.` | Context supplied to the reviewer. |
| `--timeout SECONDS` | `1800` | Positive bound for the reviewer process. |
| `--reviewer-agent {codex,claude-code}` | `codex` | Issue-reviewer runtime adapter implementation. |
| `--reviewer-model MODEL` | Runtime default | Optional model passed to the selected runtime. |
| `--runs-directory DIRECTORY` | `~/.local/state/agent-orchestra/runs` | Evidence root used when the issue was captured. |

Before and after agent execution, the command fetches the live issue. It
rejects a changed source revision and does not itself post provider feedback. The
versioned request gives both runtimes the same normalized snapshot, eight
readiness dimensions, prior iteration result when present, and only local
evidence permissions.

Each iteration writes `issue.json`, `request.json`, `result.json`, and rendered
`feedback.md` beneath `iterations/NNNNNN/`. Result verdicts are `ready`,
`changes_requested`, and `blocked`. A repeated review requires the author to
change a review-relevant issue field first.

A custom issue reviewer may be supplied for testing or integration:

```shell
agent-orchestra review-issue "$JOB_ID" -- /absolute/path/to/reviewer
```

The command receives request and result paths as its final two arguments and
must write the strict issue-review result JSON.

## `post-issue-feedback`

Publish accepted feedback as a GitHub comment or GitLab note:

```text
agent-orchestra [--database DATABASE] post-issue-feedback JOB_ID --authorize
    [--runs-directory DIRECTORY]
```

The explicit `--authorize` flag is required. Before posting, the command
re-fetches the issue and requires the reviewed digest and provider update time
to remain unchanged. Repeated calls return the recorded provider message
identity without creating another comment or note; interrupted persistence is
recovered by finding the hidden idempotency marker on the provider.

## Global settings

Agent Orchestra reads `$XDG_CONFIG_HOME/agent-orchestra/config.toml`, falling
back to `~/.config/agent-orchestra/config.toml`. The file is optional; unknown
fields, malformed TOML, invalid paths, and a non-positive duration fail closed.

```toml
[storage]
database = "~/.local/state/agent-orchestra/state.db"
runs_directory = "~/.local/state/agent-orchestra/runs"

[retention]
job_evidence_days = 90

[reviewer_sets.default]
members = [
  { id = "codex", runtime = "codex", model = "gpt-5.6" },
  { id = "claude", runtime = "claude-code" },
]
```

Precedence is command-line option, settings file, then built-in default.
Environment variables select the XDG location but do not override individual
values. `config show` reports each effective value and source without creating
or migrating the database:

```shell
agent-orchestra config show
agent-orchestra --database /var/lib/orchestra/state.db config show \
  --runs-directory /var/lib/orchestra/runs
```

Reviewer sets are ordered and named. Each set contains at least two required
reviewers with unique stable IDs. Runtime identifiers are validated against the
runtime registry, and vendor attribution is derived from that registry rather
than configured separately. Optional reviewers and quorum policies are not yet
supported; `required = false` is rejected. An explicit `[reviewer_sets]` table
must contain at least one named set. Select a set for one source-code review
batch with `run --reviewer-set NAME`; its required reviewers execute
concurrently against the same immutable diff and keep disjoint request, result,
artifact, stream, runtime-metadata, and invocation evidence. The batch advances
to approval only when every reviewer approves and otherwise stops at the review
boundary. A timed-out, failed, blocked, or invalid reviewer batch fails
terminally because reviewer-set resume is not yet supported; its `failure.json`
records the stable code `reviewer_batch_incomplete`. Worktree mutation is
rejected separately as `worktree changed during read-only review`. Reviewer-set
width is the configured member count, with one
concurrent agent process per member and no separate concurrency cap; operators
should size sets for available local resources. The canonical aggregate decision
is stored under `review-batches/`, has a human-readable aggregate artifact under
`artifacts/`, and is included in audit history. Developer
remediation and reviewer-set resume remain tracked by
[#26](https://github.com/asokolsky/agent-orchestra/issues/26). `config show`
therefore reports reviewer sets with a `status` of `"review_only"`.

## Persistent evidence retention

`prune` is a dry run unless `--apply` is present. `--older-than` accepts a
positive whole-day duration such as `30d`; otherwise the configured duration is
used. Only `failed`, `cancelled`, `superseded`, and `published` jobs are
eligible, with age taken from the matching terminal transition. Active jobs and
states requiring human action are always skipped.

```shell
agent-orchestra prune
agent-orchestra prune --older-than 30d
agent-orchestra prune --older-than 30d --apply
```

The default action expires external evidence only and retains SQLite history.
It atomically writes `.retention.json` with the policy, expiry time, and prior
integrity entries and a `pending` status before removing other files, then marks
the cleanup `completed`. `audit --verify` returns `expired` only for a completed
marker. An interrupted pending cleanup and a failed database cleanup are
retryable.
`--delete-database-records` separately requests transactional deletion of the
job, transitions, and provider actions after evidence expiry; the job is then
unavailable to normal queries and audit.

Orphans are never selected by age. `--orphans` explicitly selects unmatched
directories and reports their count against the chosen database. Application
is refused when the database has no jobs or every directory is unmatched. A
partial database read, unreadable job row, symlink, containment failure, or
non-regular file prevents unsafe deletion. Application rechecks the exact job
state and terminal transition under a database write transaction before each
filesystem mutation; a changed plan item is refused.

Evidence can contain source, issue, model, and process-stream content. Inspect
previewed paths and byte counts, restrict access to both storage locations, and
back up evidence that must survive expiry. Interrupted cleanup is retryable,
but deleted content cannot be reconstructed without an independent backup.

## Job and task views

The public hierarchy is `job` -> `task` -> `attempt`. A job is one complete
objective and workflow, a task is one durable role assignment, and an attempt
is one process execution. Four read-only, schema-version 17 JSON views expose
that hierarchy:

```text
agent-orchestra [--database DATABASE] jobs [--state STATE]... [--attention]
agent-orchestra [--database DATABASE] job JOB_ID [--runs-directory DIRECTORY]
agent-orchestra [--database DATABASE] tasks JOB_ID [--runs-directory DIRECTORY]
agent-orchestra [--database DATABASE] task TASK_ID [--runs-directory DIRECTORY]
```

`jobs` lists stored jobs newest first. Repeat `--state` to select the union of
one or more durable states. `--attention` selects the states requiring human
action: `changes_requested`, `awaiting_commit_authorization`,
`awaiting_publish_authorization`, `validation_required`, and `interrupted`.
Combining `--state` and `--attention` returns their union. An empty match is a
successful document with an empty `jobs` array. Unknown state text returns the
stable `invalid_job_state` error.

Each source-code job includes `worktree_status`: `available`, `missing`, or
`not_git_worktree`. Detection is read-only. `missing` means the recorded path is
absent; `not_git_worktree` means it exists but is not the root of a Git
worktree. Unrunnable source-code jobs remain visible in an unfiltered listing
but are excluded from `jobs --attention`.

Terminate an unrunnable source-code job explicitly with:

```text
agent-orchestra [--database DATABASE] cancel JOB_ID --reason TEXT
```

Cancellation applies only to source-code jobs. It refuses an issue-review job,
an available worktree, and any job already in a terminal state. It records the
reason on the transition to `cancelled` and never removes evidence or integrity
metadata.

If a stored job contains a state or scenario unknown to this installation,
`jobs` keeps the row in the array as `job_id`, `created_at`, and a stable
`error` object. Readable rows remain present, filters do not hide the unreadable
row, and the command exits 2. Single-job views return the same error at the
document level with the selected `job_id`.

`job` returns one job plus a `current`
array of non-terminal tasks; the array is empty when nothing is pending or
running. Source-code `job` and `tasks` documents also include every validated
aggregate reviewer decision in `review_batches`. `tasks` returns complete task
history. `task` derives the parent job from the globally unique task ID and
returns every attempt, including contained stdout and stderr paths and content.
A source reviewer task includes its iteration's aggregate decision as
`review_batch` once that batch is complete.
New reviewer batches include an aggregate `findings` array. Every finding keeps
its reviewer identity and source identifier, while its public `finding_id` is
namespaced as `{reviewer_id}:{source_finding_id}`. Batch evidence written by an
earlier version remains readable and omits this field.

```json
{
  "schema_version": 18,
  "job": {
    "job_id": "20260907T090000Z-a7f3c921",
    "state": "reviewing",
    "current": [
      {
        "task_id": "20260907T090000Z-a7f3c921:000003-reviewer",
        "role": "reviewer",
        "attempt": 2,
        "status": "running",
        "conclusion": null,
        "reviewer_id": "security"
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
| `review_batches` | Array | Validated aggregate reviewer decisions, ordered by iteration; present in source-code `job` and all `tasks` views. |
| `supersedes_job_id` | String or null | Replaced terminal job, when any. |
| `scenario` | String | `local_changes` or `issue_review`. |
| `provider` / `host` | String | Issue provider identity for issue-review jobs. |
| `namespace` / `project` | String | Provider project identity for issue-review jobs. |
| `issue_number` | Integer | GitHub issue number or GitLab project-scoped IID. |
| `source_digest` | String | Immutable normalized issue scope for issue-review jobs. |
| Other fields | Mixed | Repo/worktree or issue identity, immutable scope, iteration, remote URL, and timestamps. |

| Task or attempt field | Type | Meaning |
|---|---|---|
| `task_id` | String | Globally addressable `{job_id}:{sequence}-{role}` identifier, with `-{reviewer_id}` appended for schema-5 source-review tasks. |
| `role` | String | `developer` or `reviewer` for source-code jobs; `issue_reviewer` for issue-readiness jobs. |
| `reviewer_id` | String | Stable configured reviewer identity on schema-5 source-review tasks and attempts; absent from earlier records and non-reviewer work. |
| `review_batch` | Object | Aggregate decision for this reviewer task's iteration, when complete; present only in the direct `task` view. |
| `status` | String | Task or attempt lifecycle status. |
| `attempt_id` | String | Public identifier for one process execution. |
| `attempt` | Integer | One-based attempt ordinal. |
| `legacy` | Boolean | Always `false` for validated attempt records. |
| `streams` | Object | Separate stdout and stderr objects with path, availability, and, in task-history views, content. |

`job` reads attempt manifests to derive `current`, but does not open stream
files. `tasks` and `task` include stream content.

Stable query error codes are `state_database_not_found`, `invalid_job_state`,
`unknown_job_state`, `unknown_job_scenario`, `job_not_found`,
`invalid_task_id`, `task_not_found`, and `invalid_evidence`. Errors from a
single-job query echo `job_id`; task-addressed errors echo `task_id` and also
echo the derived `job_id` when the task identifier contains one.

This is an intentional breaking migration. The former `status` and `logs`
commands and schema-7 identifier and collection fields have no
aliases. Callers must use the four commands above and the schema-17 `job_id`,
`jobs`, and `attempt_id` fields.

The new views do not reproduce the former log-filter flags. Select a task by
its stable ID, then filter the structured document with `jq`; for example,
`agent-orchestra tasks "$JOB_ID" | jq '.tasks[] | select(.role == "reviewer")'`
replaces role filtering, and
`agent-orchestra task "$TASK_ID" | jq '.task.attempts[].streams.stdout'`
selects stdout. Iteration, runtime, attempt ID, and stream are ordinary fields
in the same documents, so callers can combine filters without another CLI
schema change.

## `audit`

Reconstruct a job's durable local history and optionally verify its finalized
evidence:

```text
agent-orchestra [--database DATABASE] audit JOB_ID [--verify]
    [--runs-directory DIRECTORY]
```

The command is read-only. It does not initialize or update the database,
evidence, worktree, issue provider, or remote repo. It reports source-code and
issue-review jobs from the same schema-15 audit document and never contacts
GitHub or GitLab.

For source-code jobs, audit reports `worktree_missing` or
`worktree_not_git_worktree` as a finding. This observation neither changes the
job state nor makes the audit command itself fail.

Without `--verify`, indexed evidence has status `not_verified` and the document
omits `result`. With `--verify`, the command hashes every finalized file,
validates canonical JSON identity and correlation, checks transition scope
digests, and adds `result`. Live process streams are not read or hashed; they
appear as `in_progress` until their invocation completes.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Independent audit output contract; currently `15`. |
| `job` | Object | Scenario-specific identity, immutable scope, state, and timestamps. |
| `transitions` | Array | Ordered SQLite state history with the scope digest and optional reason at each transition. |
| `operations` | Array | Commit authorization, commit, publish authorization, and publication views derived from transitions. |
| `tasks` | Array | Ordered roles and attempts; stream paths are included but stream contents are not. |
| `evidence` | Array | Job-relative type, path, size, recorded digest, finalization time, and verification status. |
| `integrity` | Object | Integrity schema and nullable `backfilled_at` provenance marker. |
| `history` | Array | Canonical message and issue-iteration summaries, including findings, dispositions, and validation outcomes. |
| `provider_actions` | Array | Persisted issue-provider writes; empty for source-code jobs. |
| `findings` | Array | Independently aggregated objects containing `code`, `message`, and optional `path`. |
| `result` | String | Present only with `--verify`: `failed`, `unverifiable`, `incomplete`, or `verified`. |
| `error` | Object or null | Command-level lookup or containment failure, otherwise `null`. |

Result precedence is `failed`, `unverifiable`, `incomplete`, then `verified`.
Detected modification, escape, malformed canonical JSON, or correlation failure
is `failed`. A missing, malformed, or backfilled integrity index, or a
transition without a scope digest, is `unverifiable`. Sound evidence with live
streams is `incomplete`; fully finalized sound evidence is `verified`.
An unrecognized persisted transition state or scenario is also
`unverifiable`; the raw row remains in `transitions` and the rest of the audit
document is still reported.

Stable finding codes are `integrity_index_missing`,
`integrity_index_malformed`, `integrity_index_backfilled`,
`duplicate_evidence_identity`, `evidence_missing`, `evidence_modified`,
`evidence_unreadable`, `evidence_path_escape`, `unindexed_evidence`,
`unknown_canonical_evidence`, `evidence_type_mismatch`, `invalid_canonical_json`,
`invalid_invocation_evidence`, `job_id_mismatch`,
`iteration_mismatch`, `scope_digest_mismatch`, `source_digest_mismatch`,
`message_sequence_mismatch`,
`source_identity_mismatch`, `task_id_mismatch`, `attempt_id_mismatch`,
`role_mismatch`, `message_correlation_failure`,
`unknown_transition_state`, `unknown_transition_scenario`, and
`transition_digest_missing`. Lookup failures use the same
`state_database_not_found`, `unknown_job_state`, `unknown_job_scenario`,
`job_not_found`, and `invalid_evidence` error
objects as the other job views.

```shell
agent-orchestra audit "$JOB_ID"
agent-orchestra audit "$JOB_ID" --verify
agent-orchestra audit "$JOB_ID" --verify \
  --runs-directory /var/tmp/orchestra/runs
```

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
| `--runs-directory RUNS_DIRECTORY` | `~/.local/state/agent-orchestra/runs` | External evidence root; timestamp-shaped job IDs are stored under internal `YYYY/MM/DD` shards. |

The state database and evidence directory must remain outside the target worktree.
New evidence is stored at `RUNS_DIRECTORY/YYYY/MM/DD/JOB_ID`, where the UTC
date is derived from the opaque job ID. Evidence written directly beneath the
runs directory by an older release may be unreachable when its ID has the
timestamp shape; missing evidence is reported through the command's normal
error document.
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
  "schema_version": 18,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": "awaiting_commit_authorization",
  "error": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | Integer | Version of this CLI output contract; currently `18`. |
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
  "schema_version": 18,
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

For an issue-review job in `failed` or recoverable `reviewing`, `resume` reads
the persisted issue-review request and latest completed attempt, then retries
the same built-in reviewer runtime and requested model. Custom reviewer
commands are not persisted and must instead be supplied again with
`review-issue`.

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
  "schema_version": 18,
  "job_id": "20260903T194500Z-a7f3c921",
  "state": "awaiting_commit_authorization",
  "error": null
}
```

An expected failure also remains JSON on stdout and exits 2:

```json
{
  "schema_version": 18,
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
`resume_scope_changed`, `resume_interrupted`,
`resume_execution_failed`, `resume_activation_uncertain`, `resume_cancelled`, and
`resume_evidence_invalid`.
Historical jobs whose
`execution.json` lacks the version 2 resume context fail closed with
`resume_metadata_unsupported`; start an explicitly linked replacement with
[`enqueue-local --supersedes`](#enqueue-local) only after the old job is
terminal. Valid version 3 reviewer-set execution records preserve completed
responses and retry only incomplete members with incremented attempt ordinals.
`audit --verify` validates each reviewer-qualified request, result, artifact,
and invocation together with the canonical aggregate decision and its member
result references.

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
| `--skill-home RUNTIME=PATH` | Runtime registry environment/default | Override a registered runtime configuration root; repeat as needed. |

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
  --skill-home codex=/var/tmp/codex
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
