# Subagent role publication decision

Agent Orchestra does not publish its developer or reviewer roles as Claude Code
or Codex subagent definitions. Skills remain the only installed form of portable
role content.

## What subagents provide

Both supported runtimes can start specialized agents in separate context
windows.

Product | Definition | Location |
--------|------------|----------|
Claude Code| [MD with YAML frontmatter](https://code.claude.com/docs/en/subagents) | `.claude/agents/` or `~/.claude/agents/` |
Codex | [TOML](https://learn.chatgpt.com/docs/agent-configuration/subagents) | `.codex/agents/` or `~/.codex/agents/` |

These definitions can select role
instructions, models, skills, and some capability controls.

## Subagent vs Agent Orchestra job

Aspect | Subagent | Agent Orchestra job
-------|----------|--------------------|
Owner | The parent vendor harness schedules and governs it. | Agent Orchestra owns the workflow and its durable state. |
Purpose | Delegates specialized work within one vendor session. | Tracks one objective through a complete review workflow. |
Execution isolation | Runs in a separate context window managed by the parent harness. | Starts a separate runtime process for each role attempt. |
Identity and state | Uses vendor session and subagent state. | Has a permanent job ID and SQLite-backed workflow state. |
Protocol | Returns through the vendor's session-specific mechanism. | Exchanges correlated, versioned JSON messages. |
Approval | Does not bind approval to an independently verified scope. | Binds approval to one immutable diff digest. |
Evidence | Exposes whatever transcript and logs the vendor retains. | Persists canonical messages, artifacts, streams, and an integrity index. |
Authorization | Inherits controls that the parent session may widen or override. | Enforces separate capability gates for editing, committing, publishing, and cleanup. |
Recovery | Depends on the parent harness's session and scheduling behavior. | Resumes the same job with retained task and attempt history. |
Portability | Uses a vendor-specific definition and behavior. | Uses vendor-neutral role contracts with runtime-specific adapters. |

Subagent context isolation is useful inside an interactive vendor session, but
it does not provide the contract of an Agent Orchestra job.

## Why another install target is not justified

A published subagent could execute the role instructions directly, but on its
own it would run outside Agent Orchestra's controls. Following the same
instructions is not enough: a developer or reviewer task also requires a
canonical correlated request and response, exact-diff validation, durable task
and attempt evidence, bounded recovery, and separate authorization gates. A
subagent would satisfy that contract only if an Agent Orchestra adapter launched
it, validated its response, and recorded the attempt. Without such an adapter,
the subagent would have to invoke `agent-orchestra`, wait for the job, and return
its identifier and state.

The job already creates fresh role contexts, so that wrapper would improve only
the interactive session's context usage. The benefit does not justify
maintaining two generated vendor formats and another installation lifecycle.

More importantly, a generated definition could not enforce the same capability
ceiling as the runtime manifests:

- Claude Code supports tool allowlists, denied tools, skill preloading, and
  permission modes, but the parent session can override a subagent's permission
  mode. Its available tools also depend on the parent session and whether the
  subagent runs in the foreground or background.
- Codex custom agents can set `sandbox_mode`, MCP servers, and skill
  configuration, but live sandbox and approval choices from the parent turn are
  reapplied when the agent starts, even when its file declares different
  defaults. Codex also notes that the custom-agent file format may evolve.

Projecting the packaged runtime profiles into those definitions would therefore
create an appearance of parity without enforcing it. Drift tests could compare
generated files with their source, but they could not make a parent-controlled
vendor session equivalent to the bounded process and fixed arguments that Agent
Orchestra launches.

## Consequences

- `RuntimeDefinition` does not gain a subagent format or installation target.
- `skills install` continues to install only the canonical, vendor-neutral
  skills.
- No generated `.claude/agents/` or `.codex/agents/` files are packaged.
- Subagents do not become runtimes, adapters, or workflow participants.
- The CLI schema, evidence layout, stable error codes, and audit contract do not
  change.

Reconsider this decision only if measured demand shows that the human-in-CLI
workflow materially benefits from the wrapper and both vendors expose stable,
testable controls that cannot widen the role beyond its Agent Orchestra runtime
profile. Adoption would still require one neutral source for each role, generated
vendor definitions, drift tests, and an integration check proving that the
wrapper starts an evidence-producing job rather than reviewing in its own chat
context.
