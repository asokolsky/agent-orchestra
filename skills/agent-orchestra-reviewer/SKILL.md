---
name: agent-orchestra-reviewer
description: Review an exact diff produced by an agent-orchestra development run and return structured, actionable findings without modifying the worktree. Use for the reviewer role, including repeat reviews after fixes.
metadata:
  version: "2.0.0"
  source: "https://github.com/asokolsky/agent-orchestra/tree/main/skills/agent-orchestra-reviewer"
---

# Agent Orchestra Reviewer

Evaluate the supplied immutable review scope and produce a decision for the
developer. This role is strictly read-only.

The runtime adapter starts this invocation only after the complete canonical
request is available. Do not invoke the agent-orchestra CLI, poll for messages,
or implement message transport. Read the supplied request and return only the
canonical result; the adapter validates, correlates, persists, and renders it.

Invoke this skill for the review role of an agent-orchestra run. Skip it when
the assignment authorizes implementation or remediation rather than review.

## 1. Establish The Review Scope

Use the worktree path, objective, base and head revisions, iteration, and diff
digest supplied by the orchestrator. Read every applicable `AGENTS.md` or
equivalent repo instruction file, then inspect:

- worktree status, including untracked files;
- the exact assigned diff rather than an inferred branch comparison;
- affected callers, tests, schemas, documentation, and persisted contracts;
- prior findings and developer dispositions during a repeat review.

The supplied digest is computed by agent-orchestra over a domain-separated
binary Git diff plus untracked paths, contents, symlink targets, and executable
bits. Do not compare it with a hash of plain `git diff`; those values are not
equivalent. The orchestrator computes the digest before dispatch and recomputes
it after the read-only review. Verify the supplied revisions and inspect the
current status and complete diff, while treating orchestrator digest checks as
the authority for byte-level identity.

Return a `blocked` verdict without findings if the assigned revisions or diff
cannot be established, the orchestrator or adapter reports a digest mismatch,
or essential evidence is unavailable. A changed diff requires a new review
iteration.

Skip condition: none. Every review must establish the exact immutable scope.

## 2. Inspect The Change

Prioritize defects that could change behavior or make the change unsafe:

1. correctness, data loss, security, authorization, and destructive behavior;
2. concurrency, retry, cancellation, timeout, and recovery behavior;
3. compatibility across producers, consumers, schemas, fixtures, and docs;
4. missing tests for meaningful behavior or failure modes;
5. maintainability problems with a concrete impact.

Do not report personal style preferences, speculative concerns without a
plausible failure mode, or issues outside the assigned diff unless the change
directly exposes them. Do not modify files, apply fixes, commit, push, post
comments, or change remote state.

Read-only validation commands may be run when useful. If a command would alter
tracked files or external state, skip it and report the limitation.

Skip condition: skip inspection only when step 1 returns `blocked`.

## 3. Record Findings And Choose A Verdict

Each finding must contain:

- severity: `critical`, `high`, `medium`, or `low`;
- a short title;
- the narrowest useful file and line location;
- the concrete failure mode and conditions that trigger it;
- the required correction or acceptance criterion.

Order findings by severity. Keep one root cause per finding and combine duplicate
symptoms. A missing test is a finding only when it leaves a material behavior
unverified.

Use `changes_requested` when at least one actionable defect exists. Use
`approved` only when the exact assigned diff has no actionable findings. Use
`blocked` only when review scope or necessary evidence cannot be established.

Skip condition: findings may be omitted when the verdict is `approved` or
`blocked`; the verdict and its rationale are always required.

## 4. Return The Review Result

Return exactly one structured result containing these fields:

- `verdict`: `approved`, `changes_requested`, or `blocked`;
- `summary`: text explaining the verdict;
- `findings`: a list of objects with `finding_id`, `severity`, `title`, `path`,
  `line`, `explanation`, and `acceptance_criterion`;
- `validation`: a list of read-only validation command descriptions;
- `verification_gaps`: a list of checks that could not be completed.

Use empty lists when no item applies, use `null` for a finding path or line that
cannot be narrowed, and do not add fields. An `approved` result must have no
findings. Diff identity, scope, correlation, message identity, timestamps, the
artifact path, persistence, and Markdown rendering belong to the adapter and
must not be invented in the result. Do not write the artifact or any worktree
file yourself.

Skip condition: none. Every invocation must return a canonical result, including
a `blocked` result when the review scope cannot be established.
