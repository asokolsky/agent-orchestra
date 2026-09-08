# Bundled skills

This directory holds the canonical role instructions Agent Orchestra installs
for supported agent runtimes.

- `agent-orchestra-developer` — implement an assigned change or address reviewer
  findings inside a managed worktree.
- `agent-orchestra-reviewer` — review an exact diff and return structured
  findings without modifying the worktree.

Install them with `agent-orchestra skills install`.

Each `SKILL.md` is consumed verbatim by the agent a runtime adapter launches.
It describes only what that agent must do with a request it has already been
given. Invocation mechanics belong to the caller and must not be added to a
`SKILL.md`, where they would be dead weight in every orchestrated run and would
blur the boundary the reviewer skill states explicitly: the agent does not
invoke the CLI, poll for messages, or implement message transport.

## Running a skill by hand

Reproducing a verdict, debugging an adapter, or evaluating a prompt change is
easier outside a full run. The orchestrator normally assembles the request, so a
by-hand invocation has to supply the same material: the skill text, the review
scope, and an output schema.

Give standard input a source that reaches end of file. A runtime reads standard
input whenever it is a non-TTY pipe, so an invocation whose standard input is an
open pipe with no writer hangs until its timeout rather than failing. The only
symptom is a single stderr line, `Reading additional input from stdin...`, which
is also printed on success. See openai/codex#20919 and the process contract in
`src/agent_orchestra/adapter/README.md`.

Redirecting a file satisfies this, which is what the example below does to
supply the assembled prompt. When the prompt is supplied another way, redirect
from `/dev/null` instead. What must not happen is inheriting an open standard
input from the parent shell.

```shell
# Build a prompt: the skill text, then the request the orchestrator would supply.
{
  cat skills/agent-orchestra-reviewer/SKILL.md
  printf '\n## Review request\n\n- worktree: %s\n- objective: %s\n\n' \
    "$PWD" 'Review the assigned diff.'
  git status --porcelain=v1 --untracked-files=all
  git diff HEAD
} > /tmp/review-prompt.txt

# Standard input is the prompt file, so it reaches end of file. Without a
# redirection the runtime would inherit this shell's standard input and wait.
codex exec \
  --ephemeral --ignore-user-config --sandbox read-only \
  --cd "$PWD" --skip-git-repo-check --color never \
  < /tmp/review-prompt.txt
```

A by-hand run reproduces the agent's reasoning only. Diff identity, correlation,
persistence, integrity indexing, and Markdown rendering belong to the adapter
and the orchestrator, so a result produced this way is not run evidence.
