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

## Design

### Principles

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

### Message representation

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

### JSON envelope

Every message uses the same top-level envelope:

```json
{
  "schema_version": 1,
  "message_id": "3bfc3f23-c25a-4b62-a7bf-610a54206f53",
  "in_reply_to": null,
  "run_id": "416c0c1a-43a5-405c-8804-c9b18ea38462",
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
| `run_id` | Persistent orchestration run UUID. |
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

### Message payloads

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

### Request and response validation

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

The messages below are the version 1 JSON message types defined in the design
section. File transport, message persistence, and provider adapters are not
implemented yet. Until they are, the orchestrator passes part of the same
information through typed Python requests and results; Markdown remains only a
human review artifact.

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
