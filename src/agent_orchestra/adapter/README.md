# Adapter layer

This package isolates runtime-specific process behavior from Agent Orchestra's
canonical workflow contracts.

`base.py` defines the vendor-neutral abstract role interfaces. Role modules
define shared requests, prompts, message handling, and errors. Runtime modules
implement the interfaces and own executable discovery,
command construction, sandbox configuration, timeouts, output-envelope parsing,
and runtime metadata. Orchestration code selects an implementation but does not
encode runtime-specific commands.

For issue review, `issue_reviewer.py` defines the common issue-reviewer
assignment. This role evaluates issue prose and returns feedback to an issue
creator; it is separate from the source-code reviewer and source-code developer.
`codex.py` and `claude_code.py` provide concrete implementations of
`IssueReviewerAdapter.execute()`. Both receive the same versioned request and
must return the same canonical result shape.

For diff-scoped code review and development, runtime modules implement
`ReviewerAdapter` and `DeveloperAdapter`; `developer.py` retains only the
developer-specific canonical request and response helpers.

Provider access is separate from agent runtime selection. GitHub and GitLab
issue capture and publishing implement the `IssueProvider` interface in
`issue_sources.py`; provider selection does not leak into the reviewer
adapters. An issue reviewer sees only the normalized snapshot and never receives
credentials or permission to write to a provider.

The `AgentAdapter` protocol in `agent_orchestra.agents` is the orchestration-side
process boundary used to launch a configured adapter command and collect attempt
evidence. The abstract classes in this package are the runtime-side role
contracts implemented behind those commands; the two layers intentionally have
different request shapes and responsibilities.
