# Developer role

The `developer` role implements an objective or fixes review findings in one
assigned worktree. It may use only the capabilities listed in its request and
returns a structured handoff for review.

This contract is vendor-neutral. Codex and Claude Code execute it through their
respective runtime adapters and the same canonical
`agent-orchestra-developer` skill.

## When the role runs

The orchestrator may dispatch a developer:

- when the run enters the `developing` state, to implement the original
  objective;
- after the run enters the `changes_requested` state and returns to
  `developing`, to evaluate and remediate review findings;
- for a separately authorized lifecycle action only when that exact capability
  is explicitly included in the assignment.

The initial invocation may be skipped when a run begins with completed
uncommitted changes. In that case, those changes serve as the developer
handoff, but they still require an immutable-diff review.

## Assignment

A development assignment identifies the run and iteration, absolute worktree
path, Git and diff scope, objective, timeout, and allowed actions. A remediation
assignment also identifies the prior review message and its artifact.

Before editing, the developer must:

1. Read applicable repo instructions.
2. Inspect status, the current diff, and untracked files.
3. Preserve pre-existing changes and distinguish them from new work.
4. Confirm that review feedback, when supplied, matches the current iteration
   or diff.

Missing scope, stale feedback, or a collision with work that cannot be safely
preserved produces a `blocked` result rather than an inferred assignment.

## Capabilities and restrictions

At most, the role may read and edit the assigned worktree, run local checks,
and write declared run artifacts. The request's `allowed_actions` may narrow
that set.

The developer must not infer permission to commit, push, create or update a
pull request, post remotely, merge, or remove a worktree. Each is a separate
capability and authorization decision. The role must never discard existing
work with reset, restore, checkout, stash, cleaning, or an equivalent action.

All edits and local validation occur inside the assigned worktree. A built-in
developer sandbox may use outbound network access to fetch dependencies needed
by project validation, but that technical access does not authorize remote
lifecycle writes. Workflow messages, review artifacts, process logs, and other
run evidence remain outside the worktree unless the assignment explicitly
requires a repo artifact. Runtime-only evidence channels are not inherited by
commands launched inside the developer sandbox.

## Implementation and remediation

The developer makes the smallest complete change that meets the objective and
follows the repo's rules. Contract changes include matching tests and docs.

During remediation, every finding is evaluated rather than accepted blindly.
The handoff records each finding as addressed, rejected with a concrete
rationale, or blocked on a decision. Any edit creates a new diff digest and
therefore requires a new review; prior approval never carries across a changed
digest.

Validation must be proportional to the change and include the repo's required
checks. Before handoff, the developer inspects the complete status and diff for
unrelated changes, generated artifacts, credentials, and formatting noise.

## Result

The canonical `developer_handoff` contains:

- status: `ready_for_review`, `blocked`, or `failed`;
- a summary of the implementation;
- files changed;
- validation commands and outcomes;
- dispositions for prior findings, when applicable;
- remaining risks or decisions.

`ready_for_review` is invalid when required validation failed or was omitted
without a documented reason. Unless commit authorization was explicitly
included, validated changes remain uncommitted.

The versioned message fields and lifecycle transitions are defined in
[Design and message contract](design.md). The general separation of roles,
runtimes, adapters, and capabilities is defined in [Concepts](concepts.md).
The executable procedure is the canonical
[`agent-orchestra-developer` skill](../skills/agent-orchestra-developer/SKILL.md).
