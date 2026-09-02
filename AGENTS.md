# Repository guidance

## Development workflow

- Use the linked worktree assigned to the task. Preserve pre-existing changes
  and inspect `git status` before and after work.
- Use `mise` for routine project commands.
- After the final edit, run:
  - `mise run format`
  - `mise run lint`
  - `mise run mypy`
  - `mise run tests`
  - `mise run build`
- Run `git diff --check`, inspect the complete diff and untracked-file list, and
  confirm generated build output remains ignored.
- Do not commit, push, create or update a pull request, post remotely, merge, or
  remove a worktree without explicit authorization for that exact action.

## Python and naming

- Use Python 3.14 and the uv, Ruff, mypy, pytest, and mise configuration in this
  repo.
- Use `agent_orchestra` for the Python import package.
- Use `agent-orchestra` for the distribution and user-facing CLI.
- New Python files require a module docstring, applicable class and function
  docstrings, and typed parameters and return values.

## Workflow and message contract

- Follow the design contract in [docs/design.md](./docs/design.md) and the
  scenario contracts in [README.md](./README.md). Do not duplicate their
  schemas or workflow descriptions here.
- Treat versioned UTF-8 JSON messages as the canonical machine contract.
  Markdown is a human artifact, and agent stdout/stderr are execution logs;
  neither is workflow state.
- Validate message schema, request/response correlation, run ID, iteration,
  role, and exact diff digest before changing workflow state.
- Keep approval bound to one immutable diff digest. Any change after approval
  returns the run to review before a commit or remote action.
- Write messages and artifacts atomically. Reject unknown schema fields, stale
  digests, duplicate messages, and artifact paths that escape the run directory.
- Keep run messages, artifacts, and logs outside the target worktree. Associate
  them through the run record's worktree path, repo identity, SHAs, digest, and
  iteration. Worktree cleanup must not delete run evidence automatically.

## Authorization boundaries

- Treat editing, committing, pushing, pull-request creation, remote review
  posting, approval, merging, and cleanup as separate capabilities.
- An allowed action authorizes only that exact capability. Commit authorization
  does not authorize publication.
- Reviewer operations are read-only. A reviewer must not fix files, commit,
  push, post remotely, approve remotely, or merge.
- Never discard existing work with reset, restore, checkout, stash, cleaning,
  or equivalent destructive operations.

## Agent skills

- Keep `agent-orchestra-developer` and `agent-orchestra-reviewer` vendor-neutral
  and usable by both OpenAI Codex and Anthropic Claude Code from the same
  canonical `SKILL.md`.
- Do not add product-specific skill instructions unless equivalent behavior is
  maintained for both supported runtimes.
- Follow the SOT-style skill lifecycle: maintain quoted SemVer
  `metadata.version`, canonical `metadata.source`, numbered imperative steps
  with explicit skip conditions, and the companion `SKILL-meta.md`.
- Choose a major, minor, or patch version increase according to whether a skill
  change breaks an existing caller, adds compatible capability, or only
  clarifies existing behavior.
- After changing a skill, validate it with both the local quick validator and
  the pinned Agent Skills reference validator documented in the repo history.
- Keep skill installation Python-native and standard-library-only. Do not
  introduce a Node.js, npm, or `npx` dependency.
- Ensure source and wheel distributions contain the canonical skill files, and
  verify installation for both supported runtimes when packaging changes.
