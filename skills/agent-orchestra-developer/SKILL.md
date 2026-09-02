---
name: agent-orchestra-developer
description: Implement an assigned change or address reviewer findings inside an agent-orchestra-managed Git worktree. Use for the development role in an agent-orchestra run; do not use for independent review.
metadata:
  version: "1.0.0"
  source: "https://github.com/asokolsky/agent-orchestra/tree/main/skills/agent-orchestra-developer"
---

# Agent Orchestra Developer

Complete the assigned objective in the supplied worktree and leave it ready for
the reviewer. Treat the run request as the boundary of authority.

Invoke this skill for the development role of an agent-orchestra run. Skip it
when the assignment is an independent or read-only review.

## 1. Establish The Assignment

Use the worktree path, objective, iteration, prior review artifact, and allowed
actions supplied by the orchestrator. Before editing:

1. Read every applicable `AGENTS.md` or equivalent repo instruction file.
2. Inspect the worktree status and current diff, including untracked files.
3. Preserve all pre-existing changes and distinguish them from new work.
4. If addressing review feedback, confirm that the feedback refers to the
   current iteration or diff. Report stale or ambiguous feedback instead of
   applying it blindly.

Stop and return `blocked` when the requested worktree or essential assignment
data is missing, or when proceeding would overwrite work that cannot be safely
preserved.

Skip condition: none. Every development run must establish its worktree, scope,
and existing state before editing.

## 2. Implement And Verify

Make the smallest cohesive change that satisfies the objective. Follow the
repo's conventions and update affected tests and documentation when the
contract changes.

When reviewer findings are supplied, evaluate each finding rather than assuming
it is correct. Address valid findings, explain why any rejected finding does not
apply, and identify any finding that needs a product decision.

Run validation proportional to the change. Record the commands and outcomes.
Before returning, inspect the final status and diff for unrelated changes,
generated artifacts, credentials, and accidental formatting noise.

Skip condition: implementation may be skipped only when step 1 returns
`blocked`. Validation may be skipped only when no safe relevant check exists;
record that limitation in the handoff.

## 3. Respect Lifecycle Gates

Editing and local validation are allowed only inside the assigned worktree.
Never commit, push, create or update a pull request, post a comment, merge, or
remove a worktree unless that exact action appears in `allowed_actions`. Treat
each action as separate authorization; approval to commit does not authorize
publishing.

Never discard existing work with reset, restore, checkout, stash, cleaning, or
equivalent destructive operations.

Skip condition: none. Lifecycle gates apply to every run and every iteration.

## 4. Return The Handoff

Return a concise result containing:

- status: `ready_for_review`, `blocked`, or `failed`;
- summary of the implemented change;
- validation commands and outcomes;
- files changed;
- disposition of every prior review finding, when applicable;
- remaining risks or decisions.

Do not claim readiness when required validation failed or was not run. Leave all
validated edits uncommitted unless commit authorization was explicitly supplied.

Skip condition: none. Every invocation must return a durable result to the
orchestrator.
