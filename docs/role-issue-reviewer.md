# Issue reviewer role

The `issue_reviewer` protocol role evaluates one immutable issue snapshot for
implementation readiness. It is distinct from the source-code reviewer, whose
input is an uncommitted Git diff, and from the source-code developer, who edits
a worktree.

Issue refinement uses a vendor-neutral request and result contract. The request
contains a normalized GitHub or GitLab snapshot and source digest rather than a
worktree, SHAs, or diff digest. The result verdict is `ready`,
`changes_requested`, or `blocked`. Findings identify a readiness dimension and
an issue section or field; they do not invent source paths or line numbers.

The issue reviewer is read-only and receives no provider credentials or write
capability. Codex and Claude Code implement the same abstract adapter method and
receive the same canonical request. The orchestrator performs provider reads
before and after the invocation so it can reject a result for a stale snapshot.

When changes are requested, the result goes to the
[issue creator](role-issue-creator.md). That responsibility revises the issue;
the issue reviewer does not edit its own input.
