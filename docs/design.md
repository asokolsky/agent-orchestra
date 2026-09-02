# Design and message contract

## Principles

- Use an on-demand CLI and SQLite instead of a resident service.
- Keep agent, Git provider, and worktree operations behind typed interfaces.
- Review an immutable diff digest; any code change invalidates approval.
- Store structured findings and render Markdown as a human-readable artifact.
- Treat committing and publishing as separate, explicit authorization gates.
- Preserve worktrees and changes that the orchestrator does not own.
- Make every workflow transition durable and resumable.

These choices optimize for local agents and minimum resource use. Python is the
preferred implementation language, with a toolchain based on uv, Ruff, and
mise.

## Run identity

A run ID is an immutable, repo-independent identifier with the form
`{UTC timestamp}-{random hex}`, for example
`20260902T130000Z-a7f3c921`. The timestamp makes IDs easy to sort and recognize;
the random suffix prevents collisions between concurrent processes and hosts.

Repo names and branch names are not part of the canonical identity. Both are
mutable context, neither is globally unique, and a worktree may be detached.
Including either would incorrectly imply that renaming or changing that context
changes the run's identity. Repo path, worktree path, remote URL, Git SHAs, and
any observed branch name belong in separate run metadata and human-facing
labels. Existing identifiers are treated as opaque strings so older UUID-based
runs remain readable without migration.

## Message representation

Agent-orchestra messages are versioned JSON documents encoded as UTF-8. JSON is
the canonical machine contract for assignments, handoffs, review feedback,
authorization decisions, and operation results. Markdown is a human-readable
artifact generated from structured JSON; it is never parsed to recover workflow
state or findings.

Each message is stored as one file rather than mixed into process output:

```text
.agent-orchestra/
└── runs/
    └── {run-id}/
        ├── messages/
        │   ├── 000001-development-assignment.json
        │   ├── 000002-developer-handoff.json
        │   ├── 000003-review-request.json
        │   └── 000004-review-result.json
        └── artifacts/
            └── review-0001.md
```

The orchestrator writes a request to a temporary file, flushes it, and renames
it into `messages/` atomically. The agent adapter receives the request path and
an expected response path. It translates the JSON request into the invocation
format required by Codex or Claude Code. The response is also written
atomically, validated, and accepted before the workflow state changes.

Agent process stdout and stderr are retained as execution logs only. They may
contain progress text or vendor diagnostics and are never parsed as the message
response. This prevents conversational output from corrupting the protocol.

Message files and directories are the target transport contract; they are not
implemented in the current foundation. The typed Python request and result
models are the in-process precursor to this format.

## JSON envelope

Every message uses the same top-level envelope:

```json
{
  "schema_version": 1,
  "message_id": "3bfc3f23-c25a-4b62-a7bf-610a54206f53",
  "in_reply_to": null,
  "run_id": "20260902T130000Z-a7f3c921",
  "sequence": 1,
  "iteration": 0,
  "message_type": "development_assignment",
  "sender": "orchestrator",
  "recipient": "developer",
  "created_at": "2026-09-02T13:00:00Z",
  "scope": {
    "worktree_path": "/absolute/path/to/worktree",
    "base_sha": "0123456789abcdef",
    "head_sha": "fedcba9876543210",
    "diff_digest": "sha256:5ca1ab1e..."
  },
  "payload": {}
}
```

Envelope fields have these meanings:

| Field | Contract |
|---|---|
| `schema_version` | Integer version of the JSON message schema. Version 1 is the initial contract. |
| `message_id` | Globally unique UUID for idempotency and audit history. |
| `in_reply_to` | Request `message_id` answered by this message, or `null` for an initiating message. |
| `run_id` | Persistent repo-independent UTC timestamp and random identifier. |
| `sequence` | Monotonically increasing message number within the run. |
| `iteration` | Review iteration; zero before the first review request. |
| `message_type` | One of the message types defined below. |
| `sender` / `recipient` | `orchestrator`, `developer`, `reviewer`, or `user`. |
| `created_at` | UTC RFC 3339 timestamp. |
| `scope` | Absolute worktree path and immutable Git/diff identity applicable to the message. |
| `payload` | Message-specific object. Unknown fields are rejected for the declared schema version. |

`base_sha` and `head_sha` are full Git object IDs. `diff_digest` uses the form
`sha256:{hex-digest}` and identifies the exact tracked and untracked change set.
Fields that do not yet apply are `null`; they are not omitted. Paths are
absolute so an agent invocation cannot silently depend on its current working
directory.

## Message payloads

The payload for each message type is explicit:

| Message type | Required payload fields |
|---|---|
| `development_assignment` | `objective`, `allowed_actions`, `timeout_seconds` |
| `developer_handoff` | `status`, `summary`, `files_changed`, `validation`, `finding_dispositions`, `remaining_risks` |
| `review_request` | `objective`, `allowed_actions`, `timeout_seconds`, `artifact_path`, `prior_review_path` |
| `review_result` | `verdict`, `summary`, `findings`, `validation`, `verification_gaps`, `artifact_path` |
| `remediation_assignment` | `objective`, `review_message_id`, `review_artifact_path`, `allowed_actions`, `timeout_seconds` |
| `authorization_request` | `action`, `action_parameters`, `reason`, `expires_at` |
| `authorization_decision` | `approved`, `action`, `decided_by`, `reason` |
| `operation_result` | `action`, `status`, `identifiers`, `summary`, `errors` |

`allowed_actions` is an array of exact capabilities such as `edit_worktree` or
`commit`. It is not an open-ended permission string. Commit, push, pull-request
creation, remote review posting, merge, and cleanup are separate values and
separate authorization decisions.

Validation entries use this shape:

```json
{
  "command": "mise run tests",
  "exit_code": 0,
  "summary": "25 tests passed",
  "stdout_artifact": null,
  "stderr_artifact": null
}
```

Review findings use this shape:

```json
{
  "finding_id": "F-001",
  "severity": "high",
  "title": "Approval does not match the current diff",
  "path": "src/agent_orchestra/workflow.py",
  "line": 72,
  "explanation": "The digest changed after approval.",
  "acceptance_criterion": "Return the run to review before committing."
}
```

Finding IDs are stable within a review result. A later `developer_handoff`
communicates feedback disposition without rewriting the finding:

```json
{
  "finding_id": "F-001",
  "status": "addressed",
  "summary": "Approval invalidation now requires a new digest.",
  "evidence": ["tests/test_workflow.py::test_changed_diff_invalidates_approval"]
}
```

## Request and response validation

Before invoking an agent, the orchestrator validates the complete request
against the versioned schema. Before accepting a response, it verifies all of
the following:

1. `schema_version` and `message_type` are supported.
2. `in_reply_to` names the exact request message.
3. `run_id`, `iteration`, recipient role, and diff scope match the request.
4. The response message ID and sequence have not already been accepted.
5. Every required payload field is present and no unknown field is present.
6. Every artifact path stays inside the run artifact directory.
7. An `approved` review result contains no actionable findings.
8. A changed worktree digest invalidates the response before any state
   transition or authorized side effect.

Invalid JSON, schema violations, stale digests, duplicate messages, and path
escapes are recorded as protocol failures. They are not repaired by guessing at
the agent's intent.

## Lifecycle

```text
queued
  -> preparing
  -> developing
  -> awaiting_review
  -> changes_requested -> developing
  -> approved
  -> awaiting_commit_authorization
  -> committed
  -> awaiting_publish_authorization
  -> published
```

Active states may also become `failed`, `interrupted`, `cancelled`, or
`superseded` where allowed by the workflow contract.

Approval is tied to the reviewed digest. If the diff changes after approval or
while waiting for commit authorization, the run returns to `awaiting_review`
with a new digest and review iteration.

## Workflow messages

The messages below are the version 1 JSON message types defined in this design.
The first local review step implements file transport and message persistence
through a command adapter. The default Codex adapter invokes `codex exec` with
a read-only sandbox and a strict output schema, then writes the correlated JSON
response and Markdown artifact itself. A custom command adapter may be supplied
after `--`. Other lifecycle steps still pass part of the same information
through typed Python requests and results; Markdown remains only a human review
artifact.

Every message identifies:

- the run and scenario;
- the sender and recipient roles;
- the message type and review iteration;
- the worktree and objective;
- the applicable base SHA, head SHA, and diff digest;
- the actions explicitly allowed for the recipient;
- any input or output artifact paths;
- the result status, summary, validation evidence, and errors.

The workflow uses these message types:

| Message | Sender -> recipient | Purpose |
|---|---|---|
| `development_assignment` | Orchestrator -> developer | Implement the initial objective in the assigned worktree. |
| `developer_handoff` | Developer -> orchestrator | Report readiness, changed files, validation, and unresolved risks. |
| `review_request` | Orchestrator -> reviewer | Review one exact diff digest without modifying the worktree. |
| `review_result` | Reviewer -> orchestrator | Return a verdict, summary, structured findings, and Markdown artifact. |
| `remediation_assignment` | Orchestrator -> developer | Deliver the prior review and request fixes for its findings. |
| `authorization_request` | Orchestrator -> user | Request one specific commit, publication, or remote-posting action. |
| `authorization_decision` | User -> orchestrator | Allow or deny exactly the requested action. |
| `operation_result` | Acting agent -> orchestrator | Report the commit, publication, remote post, failure, or cancellation. |

A review finding carries `severity`, `title`, `path`, optional `line`, and a
concrete explanation. The Markdown review is the human-readable projection of
the same structured result. The orchestrator gives the developer the complete
review artifact during remediation; it does not summarize away individual
findings. The developer handoff records the disposition of every finding as
addressed, rejected with rationale, or blocked on a decision.
