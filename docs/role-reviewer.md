# Reviewer role

The `reviewer` role evaluates one exact diff and returns a verdict with
structured findings. It is read-only and never fixes the change it reviews.

This contract is vendor-neutral. Codex and Claude Code execute it through their
respective runtime adapters and the same canonical
`agent-orchestra-reviewer` skill.

## When the role runs

The orchestrator dispatches a reviewer after freezing the review identity and
moving the run to the `reviewing` state. A repeat review receives a new
iteration and diff digest after remediation.

For remote pull requests, the reviewer operates on an isolated, exact-head
worktree. A provider URL or branch name alone is not an immutable review scope.

## Assignment

A review request identifies the run and iteration, objective, absolute
worktree and artifact paths, base and head SHAs, exact diff digest, timeout,
allowed actions, and any prior review artifact.

Before reviewing, the reviewer must:

1. Read applicable repo instructions.
2. Inspect worktree status, including untracked files.
3. Establish the exact assigned diff rather than infer a branch comparison.
4. Confirm that the current diff matches the assigned digest.
5. Inspect prior findings and developer dispositions during a repeat review.

The reviewer returns a `blocked` verdict without findings when the scope cannot
be established, the digest is stale, or essential evidence is unavailable. A
changed diff requires a new review iteration.

## Capabilities and restrictions

The reviewer may read the assigned worktree, run safe read-only validation, and
write the declared review artifact outside the worktree. It must not edit
files, apply fixes, commit, push, create or update a pull request, post remote
feedback, approve remotely, merge, or remove a worktree.

The runtime cannot grant more permission. If a check would alter tracked files
or external state, the reviewer skips it and records the gap.

## Review priorities

The reviewer prioritizes concrete failure modes in this order:

1. Correctness, data loss, security, authorization, and destructive behavior.
2. Concurrency, retry, cancellation, timeout, and recovery behavior.
3. Compatibility across producers, consumers, schemas, fixtures, and docs.
4. Missing tests for meaningful behavior or failure modes.
5. Maintainability problems with a concrete impact.

Personal style preferences, speculative concerns without a plausible failure
mode, and unrelated pre-existing issues are not findings.

Each finding records severity, a short title, the narrowest useful file and
line location, the triggering conditions and concrete impact, and the required
correction or acceptance criterion. Findings are ordered by severity, keep one
root cause each, and use stable IDs within the result.

## Verdict and result

The reviewer chooses exactly one verdict:

- `approved` when the exact assigned diff has no actionable findings;
- `changes_requested` when at least one actionable defect exists;
- `blocked` when the scope or necessary evidence cannot be established.

The canonical `review_result` contains the verdict, reviewed digest, summary,
structured findings, validation evidence, verification gaps, and artifact
path. The Markdown artifact is a human-readable rendering of that result, not
workflow state.

Approval applies only to the reviewed digest. It does not authorize a commit,
remote review, publication, approval on a provider, or merge. The orchestrator
must reject a result with a mismatched run, iteration, correlation, or digest.

The versioned message fields and lifecycle transitions are defined in
[Design and message contract](design.md). The general separation of roles,
runtimes, adapters, and capabilities is defined in [Concepts](concepts.md).
The executable procedure is the canonical
[`agent-orchestra-reviewer` skill](../skills/agent-orchestra-reviewer/SKILL.md).
