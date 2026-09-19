# Omnigent comparison

Omnigent gives us a useful comparison. Its Polly example pairs coding agents
with reviewers from other vendors, much as Agent Orchestra can. The difference
is what remains after the agents finish: Agent Orchestra binds approval to one
exact diff, validates the responses against schemas, and keeps evidence on disk
that can be checked later.

This document records what we learned from that comparison and which ideas we
decided to adopt.

## What was compared

[Omnigent](https://github.com/omnigent-ai/omnigent) is an Apache-2.0
meta-harness over Claude Code, Codex, and other coding agents. It is roughly an
order of magnitude larger than this project. Its
[`examples/polly`](https://github.com/omnigent-ai/omnigent/tree/7499064/examples/polly)
agent plans a goal, delegates implementation to coding sub-agents in parallel
Git worktrees, and routes each diff to a reviewer from a different vendor than
the one that wrote it. The reviewer reports and never edits; the human merges.
That is this project's shape, arrived at independently.

## What the comparison establishes

Aspect | Polly | Agent Orchestra
-------|-------|----------------
Cross-vendor rule | Natural language in [`config.yaml`](https://github.com/omnigent-ai/omnigent/blob/7499064/examples/polly/config.yaml), with `allowed_purposes` per sub-agent as the only structural control. | Operator choice per role, recorded per attempt and reportable afterwards.
Approval scope | No diff digest; [`git_worktree.py`](https://github.com/omnigent-ai/omnigent/blob/7499064/omnigent/host/git_worktree.py) manages worktrees by branch name, with no SHA recorded as a contract. | Approval is bound to one diff digest and invalidated when the digest changes.
Response shape | Whatever the harness returns. | Validated against a JSON schema before it can decide anything.
Audit | No document verifiable against on-disk hashes after the fact. | Audit document verifiable against the evidence index and hashes.
Interruption | Sessions resume as conversations. | The workflow is reconstructed from durable canonical messages and attempt records.
Harness breadth | About a dozen behind one interface. | Two, behind a registry built to hold more.
Installation | `curl \| sh`, Homebrew, or `uv tool install`. | A mise-managed source checkout.

Both tools can arrange cross-vendor review. Agent Orchestra additionally makes
the review scope and result verifiable after the run.

## How we evaluated the lessons

Agent Orchestra invokes each vendor runtime as an external process. It can
record what went into that process and what came back, but it cannot enforce
controls inside a loop owned by the vendor.

That is the same reasoning already applied twice: the
[OpenAI Agents API runtime decision](openai-agents-api.md) keeps a vendor
mechanism behind our boundary rather than above it, and runtime independence is
observed and reported rather than enforced, as [design](design.md) records. It
decides the cost question below the same way, and it is why adopting a
policy-interception engine is a non-goal: we do not own the loop inside a
vendor's harness. The decisions below stick to behavior the saved evidence can
support.

This also corrects a claim made while opening the comparison. It is too strong
to say this project's cross-vendor discipline lives in code and schema. What
lives in code is the record of which runtime performed which role; the choice
itself is the operator's.

## Lessons

### Cost is recorded, not bounded — adopted in part

Omnigent ships [`cost_budget`](https://github.com/omnigent-ai/omnigent/blob/7499064/docs/POLICIES.md)
as a builtin policy with a hard `max_cost_usd` and soft `ask_thresholds_usd`,
enforced by intercepting tool calls inside a harness it owns. We have two nested
deadlines and no notion of spend.

Recording consumption is adopted. The gap is smaller than it appeared: the
Claude adapter already reads `total_cost_usd` from the result envelope and
folds it into a diagnostics string, discarding the structure. An attempt should
carry usage the way it carries model identity, with a status distinguishing a
runtime that reports it from one that does not, exactly as
`effective_model_status` already distinguishes reported from unavailable model
provenance. [Issue #151](https://github.com/asokolsky/agent-orchestra/issues/151)
tracks that work.

Enforcing a budget is declined. We cannot intercept a vendor's tool calls, and a
ceiling we cannot enforce mid-run would stop a job only between attempts, after
the spending has happened. A recorded total is honest about what it is; an
unenforceable budget is not.

### A capability bench — adopted

`RuntimeDefinition` declares:

- `reports_runtime_metadata`;
- `manifest_placeholders`;
- `skill_home_directory`; and
- adapter paths.

Until recently, nothing verified those claims against a real runtime. Omnigent's
[`tests/harness_bench`](https://github.com/omnigent-ai/omnigent/tree/7499064/tests/harness_bench)
checks each harness's declared capability matrix against observed behavior.

This is now cheaper than it looked when the comparison was written. The opt-in
live suites already carry the provider-neutral half — one `LiveRuntime`
description per runtime and shared scenario assertions — and
`effective_model_status` is already a declared capability checked against an
installed CLI. A bench extends that description rather than introducing a
subsystem, so it does not wait on a wider runtime roster.
[Issue #152](https://github.com/asokolsky/agent-orchestra/issues/152) tracks the
bench.

### Harness breadth — declined as a goal

Omnigent supports about a dozen runtimes; we support two. The registry was built
to hold more and the adapters are the cost, not the design.

Breadth for its own sake is declined. A runtime earns its adapter by being one
somebody reviews with, and each one added is a live suite, a skill install path,
and a capability claim to keep honest. The bench above is what makes a wider
roster affordable when a reason for one arrives.

### Installation — publish to PyPI

Omnigent is easier to install. It supports a shell installer, Homebrew, and
`uv tool install`. Agent Orchestra currently expects you to clone the repo and
use mise.

Agent Orchestra can already be built and installed as a Python package. The
next step is to publish that package to PyPI so you can run
`uv tool install py-agent-orchestra` without cloning the repo.
[Issue #153](https://github.com/asokolsky/agent-orchestra/issues/153) tracks
that work.

### How the project describes itself — adopted

The README keeps its conversational invitation to combine agents from different
vendors. It then explains what Agent Orchestra adds to that collaboration: the
exact diff, validated responses, and evidence you can inspect afterwards. The
primer makes the same point before starting the workflow.

### Using Agent Orchestra from another tool — no change for now

Another orchestration tool could call Agent Orchestra to run a review and
receive verified evidence. The CLI already provides that entry point. No extra
integration is planned until a real caller needs something the CLI does not
provide.

## What changes

- The docs distinguish cross-vendor collaboration from the evidence Agent
  Orchestra can verify after a run.
- [Issue #151](https://github.com/asokolsky/agent-orchestra/issues/151) adds
  structured usage data to attempt records where a runtime reports it.
- [Issue #152](https://github.com/asokolsky/agent-orchestra/issues/152) adds a
  live bench for declared runtime capabilities.
- [Issue #153](https://github.com/asokolsky/agent-orchestra/issues/153) publishes
  Agent Orchestra to PyPI for simpler installation.

## What does not change

- No spend ceiling is added beside the timeouts.  It becomes useful if adapters
  can report consumption during a run rather than after it, since only then can
  a bound stop anything.
- No policy-interception engine is added.
- No new runtime is added only to match another project's roster.
- This comparison does not change:
  - the CLI schema;
  - the evidence layout;
  - stable error codes; or
  - the audit contract.
