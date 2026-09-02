---
name: agent-orchestra-reviewer
description: Review an exact diff produced by an agent-orchestra development run and return structured, actionable findings without modifying the worktree. Use for the reviewer role, including repeat reviews after fixes.
metadata:
  version: "1.0.0"
  source: "https://github.com/asokolsky/agent-orchestra/tree/main/skills/agent-orchestra-reviewer"
---

# Agent Orchestra Reviewer

Evaluate the supplied immutable review scope and produce a decision for the
developer. This role is strictly read-only.

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

Return `blocked` without a verdict if the assigned revisions or diff cannot be
established, the worktree no longer matches the assigned digest, or essential
evidence is unavailable. A changed diff requires a new review iteration.

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

## 4. Write The Review Artifact

Write the Markdown artifact to the path supplied by the orchestrator. Also
return a concise result containing the verdict, reviewed diff digest, summary,
structured findings, validation performed, and remaining verification gaps.

Skip condition: skip writing a file only when the orchestrator did not supply an
artifact path; always return the review result directly.
