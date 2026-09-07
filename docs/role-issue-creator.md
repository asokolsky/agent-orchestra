# Issue creator responsibility

The issue creator writes or revises issue prose in response to readiness
feedback from the [issue reviewer](role-issue-reviewer.md). This is the
issue-refinement counterpart to the source-code developer, but it changes a
provider issue rather than an assigned worktree.

Issue-creator work may clarify the problem, narrow scope, record constraints
and dependencies, expose risks, or make acceptance criteria testable. Each
revision changes the captured source digest and permits another issue-review
iteration. It does not alter an earlier review or its evidence.

Agent Orchestra does not currently dispatch an issue-creator agent. A person or
external system revises the GitHub or GitLab issue. The CLI can publish the
issue review as a provider comment only through a separate explicit
authorization; permission to publish feedback is not permission to edit the
issue description.

The issue creator is distinct from the source-code developer. Readiness
feedback concerns the specification before implementation, while source-code
review findings concern an exact uncommitted diff after implementation work.
