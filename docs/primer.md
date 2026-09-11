# Primer

This takes you from an unreviewed change to a review you can act on.

For the exhaustive option tables, see the [CLI reference](cli.md). For why the
tool is shaped this way, see the [design](design.md). This page teaches the
workflow and links onward rather than repeating either.

## Before you start

You need three things:

- A Git repository containing the change you want reviewed.
- A working `codex` or `claude` command, authenticated.
- The `agent-orchestra` command, which is not published to a package index yet.
  The next section builds it from a checkout.

### Making `agent-orchestra` available

You also need [mise](https://mise.jdx.dev/getting-started.html), which installs
the rest of the toolchain including `uv`. Then clone this repository, enter it,
and prepare its dependencies:

```shell
git clone https://github.com/asokolsky/agent-orchestra.git
cd agent-orchestra
mise trust
mise install
uv sync --group dev
```

`mise trust` is required before the first `mise install`: mise refuses to read a
`mise.toml` it has not been told to trust.

Run the remaining commands from inside that checkout.

Run it through mise, which is how this guide invokes it throughout:

```shell
mise agent-orchestra -- --version
```

That prints `agent-orchestra 0.1.0`. The `--` separates mise's own arguments
from the command's.

Two alternatives exist if you prefer them. `uv run agent-orchestra ...` works
from the checkout without mise, and installing the built distribution
(`mise run build`, then `uv tool install dist/*.whl`) puts a plain
`agent-orchestra` on your `PATH`. All three accept identical arguments; see
[Invocation](cli.md#invocation).

## What this tool is for

You have a change in a worktree. You want an agent to review it, and you want
the review to be evidence rather than a chat log: bound to an exact diff, stored
on disk, and inspectable afterwards.

Agent-orchestra runs that review as a **job**. It records the diff digest before
the reviewer starts, runs the reviewer with only the capabilities its role
allows, validates the response against a schema, and writes every request,
result, and process stream under a runs directory outside your worktree. Nothing
is committed or published without a separate, explicit decision from you.

It is worth reaching for when you want the review recorded and repeatable. It is
not worth reaching for to ask an agent a quick question about your code.

## 1. Install the role skills

The reviewer and developer roles read their instructions from an installed
skill, so install both before the first run:

```shell
mise agent-orchestra -- skills install \
  --skill agent-orchestra-developer --skill agent-orchestra-reviewer
```

Repeat this after a skill version changes. Where it installs, what it does to an
existing installation, and how to override a runtime root are documented under
[`skills`](cli.md#skills).

## 2. Capture the change

Record the worktree's current diff as a job:

```shell
export JOB_ID="$(mise agent-orchestra -- enqueue-local /path/to/repo)"
printf '%s\n' "$JOB_ID"
```

[`enqueue-local`](cli.md#enqueue-local) prints only the job ID, so command
substitution captures it directly. It records the worktree's current diff and
starts nothing; what counts as that diff is documented with the command.

The digest captured here is what the review is bound to. If you edit the
worktree afterwards, the review no longer describes what you have.

To scan a directory of repositories and enqueue only the dirty ones, use
[`enqueue-locals`](cli.md#enqueue-locals).

## 3. Review it

```shell
mise agent-orchestra -- run "$JOB_ID" \
  --objective 'Review the queued implementation.' \
  --no-remediation
```

`--objective` is what the reviewer is asked to judge. Be specific: naming what
you are unsure about produces a more useful review than "review this".

`--no-remediation` reviews once and stops, which is what you want when you
intend to address the findings yourself. Without it, a developer is dispatched to
address what the reviewer raises. Both options, and the bounds on that loop, are
described under [`run`](cli.md#run).

To use Claude Code instead of the default Codex adapter:

```shell
mise agent-orchestra -- run "$JOB_ID" \
  --objective 'Review the queued implementation.' \
  --reviewer-agent claude-code --reviewer-model sonnet --no-remediation
```

## 4. Read the result

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

To see what stopped it, read the reviewer's Markdown review. It has Summary,
Findings, Validation, and Verification gaps sections, and the rationale for a
block may be in the summary or the gaps. `audit` reports its job-relative path
under `evidence` as a `review_artifact`, and it sits beneath the runs directory:

```shell
find ~/.local/state/agent-orchestra/runs -path "*$JOB_ID*" -name 'review-0*.md'
```

That path is the built-in default. It changes if you pass `--runs-directory` or
set `[storage].runs_directory` in the settings file, so if the search finds
nothing, check the effective value with
`mise agent-orchestra -- config show` and search there instead.

Once you have cleared the obstacle, **start a new review**: capture the
diff again with `enqueue-local` and run it. The blocked job is not resumed into a
fresh review — [`run`](cli.md#run) requires a queued job and will refuse this
one, and [`resume`](cli.md#resume) revalidates the completed blocked attempt and
returns the same `reviewing` job rather than launching another reviewer. The
blocked job stays on disk as the record of what could not be judged.

To see the findings, read the job's evidence:

```shell
mise agent-orchestra -- audit "$JOB_ID"
```

[`audit`](cli.md#audit) reports the whole job: every transition, task, attempt,
and message, with each reviewer verdict and its findings under `history`. Add
`--verify` to hash every stored file and confirm the evidence matches what was
recorded.

For a narrower look, [`job`](cli.md#job) shows current state,
[`tasks`](cli.md#tasks) shows the full role history, and [`task`](cli.md#task)
shows one task's attempts including captured stdout and stderr.

## 5. When changes are requested

The findings are yours to act on. Fix what you agree with in the worktree, then
capture and review again — a new `enqueue-local` records the new diff, and the
new job is reviewed against it. The previous job stays on disk as the record of
what was found the first time.

Two things deliberately do not happen automatically: nothing is committed, and
no finding is marked resolved on your say-so. Approval binds to the exact diff
reviewed, so changing the code invalidates it.

## Where to go next

- [CLI reference](cli.md) — every command, option, and output document.
- [Concepts](concepts.md) — jobs, tasks, attempts, roles, runtimes, adapters.
- [Workflows](workflows.md) — the state machine and its recovery paths.
- [Design](design.md) — why the tool is built this way, and the contracts it
  keeps.

Two things to look up before you build on the output:

- How long evidence is kept, and what removes it: [`prune`](cli.md#prune).
- What `schema_version` promises a program reading these documents:
  [the compatibility rule](design.md#what-a-schema-version-promises).
