#!/usr/bin/env bash

set -euo pipefail

repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$repo_root"

fail() {
  printf 'release creation refused: %s\n' "$1" >&2
  exit 1
}

branch="$(git branch --show-current)"
[[ "$branch" == main ]] || fail "current branch is $branch, expected main"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || \
  fail 'the main checkout has uncommitted or untracked files'

git fetch origin main
target="$(git rev-parse HEAD)"
remote_target="$(git rev-parse origin/main)"
[[ "$target" == "$remote_target" ]] || fail 'main is not at origin/main'

version_reader='from tools.release import project_version; print(project_version())'
version="$(uv run python -c "$version_reader")"
tag="v$version"

mise run verify-release-tag -- "$tag"
release_arguments=(
  "$tag"
  --repo asokolsky/agent-orchestra
  --target "$target"
  --title "$tag"
  --generate-notes
)
gh release create "${release_arguments[@]}"
