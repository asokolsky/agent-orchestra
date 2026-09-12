# Primer

Agent-orchestra enables collaboration of agents from multiple vendors.

This doc takes you step by step from an un-reviewed change in your repo to a
reviewed PR.

## What this tool is for

Suppose you have a change in your
[worktree](https://git-scm.com/docs/git-worktree) developed, possibly with
assistance of an agent. To improve the quality of the code it would be nice to
request a code review from another agent and then act on its findings. Rinse,
repeat, until the code is ready for a pull request.

You want:

- [evidence](concepts.md#canonical-messages-and-artifacts) rather than a chat log,
- bound to an exact [diff digest](concepts.md#jobs-tasks-and-attempts),
- stored on disk to be inspected afterwards.

Agent-orchestra runs that review as a
[job](concepts.md#jobs-tasks-and-attempts). It:

- records the diff digest before the reviewer starts,
- runs the reviewer with only the [capabilities](concepts.md#capabilities) its
  [role](concepts.md#roles) allows,
- validates the response against a schema, and
- writes every request, result, and process stream under a runs directory
  outside your worktree.

In this scenario nothing is committed or published without a separate, explicit
decision from you.

## Prerequisites

Before you begin, you need:

- Your own Git repo containing the change you want reviewed,
  [git worktree](https://git-scm.com/docs/git-worktree) workflow recommended.
- Agent [runtimes](concepts.md#runtimes) installed and authenticated, e.g. a
  working `codex` and/or `claude` session.
- [mise](https://mise.jdx.dev/) to manage the toolchain.

### Making `agent-orchestra` available

Clone it and install the toolchain:

```shell
git clone https://github.com/asokolsky/agent-orchestra.git
cd agent-orchestra
mise trust
mise install
uv sync --group dev
```

### Running agent-orchestra - option 1

Run these commands from inside the checkout.

```shell
mise agent-orchestra -- --version
```

That prints `agent-orchestra 0.1.0`. The `--` separates mise's own arguments
from the command's.

### Running agent-orchestra - option 2

Alternatively, this works from the checkout without mise:

```sh
uv run agent-orchestra --version
```

### Running agent-orchestra - option 3

Yet another option is to build the distribution and install it to put
`agent-orchestra` in your `PATH`.

```sh
mise run build
uv tool install dist/*.whl
agent-orchestra --version
```

All three accept identical arguments; see [Invocation](cli.md#invocation).

## 1. Install the role skills

Enable agents to use `agent-orchestra` for various [roles](concepts.md#roles) by
installing the [`skills`](cli.md#skills):

```shell
mise agent-orchestra -- skills install \
  --skill agent-orchestra-developer --skill agent-orchestra-reviewer
```

Repeat this after a skill version changes.

## 2. Capture the change

Record the worktree's current diff as a job:

```shell
export JOB_ID="$(mise agent-orchestra -- enqueue-local /path/to/repo)"
printf '%s\n' "$JOB_ID"
```

Or ask your agent to do exactly this step:

```text
Use agent-orchestra to capture the current uncommitted diff in /path/to/repo
as a new job. Do not start a review or modify the worktree. Return the job ID.
```

[`enqueue-local`](cli.md#enqueue-local) prints only the job ID, so command
substitution captures it directly. It records the worktree's current diff and
starts nothing. The digest captured here is what the review is bound to. If you
edit the worktree afterwards, the review no longer describes what you have.

To scan a directory of repositories and enqueue only the dirty ones, use
[`enqueue-locals`](cli.md#enqueue-locals).

## 3. Request review

Ask the default (Codex) agent to review the uncommitted change:

```shell
mise agent-orchestra -- run "$JOB_ID" \
  --objective 'Review the queued implementation.' \
  --no-remediation
```

Or ask your agent to do exactly this step:

```text
Use agent-orchestra to review job <JOB_ID> with the objective "Review the
queued implementation." Run one review without remediation. Do not modify,
commit, or publish the worktree. Return the resulting job state.
```

`--objective` is what the reviewer is asked to judge. Be specific: naming what
you are unsure about produces a more useful review than "review this".

`--no-remediation` reviews once and stops, which is what you want when you
intend to address the findings yourself. Without it, a
[developer](concepts.md#roles) is dispatched to address what the reviewer
raises. Both options, and the bounds on that loop, are described under
[`run`](cli.md#run).

To use Claude Code instead of the default Codex
[adapter](concepts.md#adapters):

```shell
mise agent-orchestra -- run "$JOB_ID" \
  --objective 'Review the queued implementation.' \
  --reviewer-agent claude-code --reviewer-model sonnet --no-remediation
```

## 4. Read the review result

The command prints a versioned JSON document ending in the job's state. These
are the outcomes you will see:

| state | meaning |
|---|---|
| `awaiting_commit_authorization` | approved; nothing has been committed |
| `changes_requested` | the reviewer wants changes |
| `reviewing` | the reviewer returned `blocked`: it could not judge the change |
| `interrupted` or `validation_required` | stopped recoverably; see [`resume`](cli.md#resume) |

A `blocked` verdict is not a failure and not a rejection. The reviewer is saying
it could not reach a judgement — a verification gap it could not close, or a
decision only you can make. For the single reviewer this guide runs, the job
stays in `reviewing` because nothing about the change has been decided.
(A reviewer set behaves differently: a batch that comes back blocked fails
terminally. See [`run`](cli.md#run).)

To see what stopped it, read the review. It has Summary, Findings, Validation,
and Verification gaps sections, and the rationale for a block may be in the
summary or the gaps. `audit` reports its job-relative path under `evidence` as a
`review_artifact`, and it sits beneath the runs directory:

```shell
find ~/.local/state/agent-orchestra/runs -path "*$JOB_ID*" -name 'review-0*.md'
```

That path is the built-in default. It changes if you pass `--runs-directory` or
set `[storage].runs_directory` in the settings file, so if the search finds
nothing, check the effective value with
`mise agent-orchestra -- config show` and search there instead.

Once you have cleared the obstacle, **start a new review**: capture the diff
again with `enqueue-local` and run it. The blocked job is not resumed into a
fresh review — [`run`](cli.md#run) requires a queued job and will refuse this
one, and [`resume`](cli.md#resume) revalidates the completed blocked attempt and
returns the same `reviewing` job rather than launching another reviewer. The
blocked job stays on disk as the record of what could not be judged.

To see the findings, read the job's evidence:

```shell
mise agent-orchestra -- audit "$JOB_ID"
```

Or ask your agent to do exactly this step:

```text
Use agent-orchestra to audit job <JOB_ID>. Summarize its state, reviewer
verdict, findings, validation, verification gaps, and evidence paths. Do not
modify the worktree or the job.
```

[`audit`](cli.md#audit) reports the whole
[job](concepts.md#jobs-tasks-and-attempts): every transition,
[task and attempt](concepts.md#jobs-tasks-and-attempts), and
[message](concepts.md#canonical-messages-and-artifacts), with each reviewer
verdict and its findings under `history`. Add
`--verify` to hash every stored file and confirm the evidence matches what was
recorded.

For a narrower look, [`job`](cli.md#job) shows current state,
[`tasks`](cli.md#tasks) shows the full role history, and [`task`](cli.md#task)
shows one task's attempts including captured stdout and stderr.

## 5. When changes are requested

The findings are yours or agent's to act on. Fix what you agree with in the
worktree, then capture and review again — a new `enqueue-local` records the new
diff, and the new job is reviewed against it. The previous job stays on disk as
the record of what was found the first time.

Two things deliberately do not happen automatically: nothing is committed, and
no finding is marked resolved on your say-so. Approval binds to the exact diff
reviewed, so changing the code invalidates it.

Ask your agent to do exactly this step:

```text
Read the review findings for job <JOB_ID>. Evaluate each finding, apply the
valid fixes to /path/to/repo, and run the repo's validation commands. Explain
any finding you reject. Do not commit or publish anything. When the worktree is
ready for another review, use agent-orchestra to capture its updated diff as a
new job and return the new job ID.
```

## 6. Authorize each Git action

Approval leaves the job in `awaiting_commit_authorization` and your worktree
exactly as the reviewer saw it. Agent-orchestra does not commit, push, or merge.
Those are yours to ask for, one at a time.

Give each state-changing operation its own instruction, and let it finish before
you give the next:

```text
Commit the validated changes with a Conventional Commit.
```

Then, only when publication is intended:

```text
Push the committed branch and create a pull request. Do not merge it.
```

Finally, only after checking the live pull-request head, checks, approvals, and
mergeability:

```text
Merge <pull-request URL> and verify the resulting default-branch commit.
```

Keeping them separate is the point, not ceremony. An agent given "commit, push,
and open a pull request" as one instruction will reasonably carry it through to
the end, and each step past the commit is harder to undo than the one before it.
The approval you are acting on binds to the diff that was reviewed, so anything
that changes the branch after it also invalidates it.

## Short-circuit the review cycle, steps 2-6

To perform steps 2-6 in a cycle, from the uncommitted change through to an open
pull request:

```text
Use agent-orchestra to review the uncommitted change. Address the feedback,
repeat the review until approved. Then commit, push, and create a pull request.
```

## Where to go next

- [CLI reference](cli.md) — every command, option, and output document.
- [Concepts](concepts.md) — [jobs, tasks, and
  attempts](concepts.md#jobs-tasks-and-attempts), [roles](concepts.md#roles),
  [runtimes](concepts.md#runtimes), and [adapters](concepts.md#adapters).
- [Workflows](workflows.md) — the state machine and its recovery paths.
- [Design](design.md) — why the tool is built this way, and the contracts it
  keeps.

Two things to look up before you build on the output:

- How long evidence is kept, and what removes it: [`prune`](cli.md#prune).
- What `schema_version` promises a program reading these documents:
  [the compatibility rule](design.md#what-a-schema-version-promises).
