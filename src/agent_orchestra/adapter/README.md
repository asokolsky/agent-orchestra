# Adapter layer

This package isolates runtime-specific process behavior from Agent Orchestra's
canonical workflow contracts.

`base.py` defines the vendor-neutral abstract role interfaces. Role modules
define shared requests, prompts, message handling, and errors. Runtime modules
implement the interfaces and own executable discovery,
command construction, sandbox configuration, timeouts, output-envelope parsing,
and runtime metadata. `registry.py` is the single source for public runtime
identifiers, vendors, supported roles, adapter implementations, module entry
points, skill homes, and metadata capability. Orchestration resolves a
`(runtime, role)` pair through that registry and does not encode runtime-specific
commands or identity branches.

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

## Child standard input must be closed

Every runtime is launched through `run_streaming_process`, which writes the
prompt to the child's standard input and then closes it. Closing is a
requirement of the process contract rather than tidiness.

A runtime that reads standard input blocks until end of file. Codex reads
standard input whenever it is a non-TTY pipe, even when the prompt is supplied
as a command argument, so a caller that leaves the pipe open deadlocks the child
until its timeout expires. The only symptom is a single stderr line,
`Reading additional input from stdin...`, which the runtime prints on success as
well and which therefore does not distinguish a hang from normal operation. This
is a known upstream defect, openai/codex#20919.

A new runtime module inherits the correct behavior by launching through
`run_streaming_process`. Do not spawn a runtime with `subprocess.run` or
`subprocess.Popen` directly; that bypasses the closing guarantee along with the
live stream tee, the timeout controller, and attempt evidence capture.

`test_streaming_process_closes_child_stdin` asserts the invariant directly, by
running a child that reads to end of file and would otherwise hang.
