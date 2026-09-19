# Repository tools

The code in this directory supports development and releases. It is kept out of
the `agent_orchestra` package because none of it is part of the installed CLI's
runtime contract.

## Release verification

`release.py` reads the package version from `pyproject.toml` and implements the
checks exposed through these mise tasks:

- `mise run verify-release-tag -- TAG` requires `TAG` to be `v` followed by the
  exact package version.
- `mise run verify-dist` checks the wheel and source distribution metadata,
  packaged manifests, and canonical role skills. It then installs the wheel in
  a temporary environment and exercises the installed CLI and skill installer.

The tests in `tests/test_release.py` build small synthetic archives around those
contracts. They also require the release workflow to pin the PyPI publisher
action to the peeled Git commit that has a matching published container image;
an annotated tag-object SHA is not a usable container tag.

## Creating a GitHub release

Run `./tools/create-release.sh` from an up-to-date, clean `main` checkout after
the version change has been reviewed and merged. The script:

- moves to the repo root so its behavior does not depend on the caller's
  current directory;
- refuses to run outside `main` or with tracked or untracked work present;
- fetches `origin/main` and requires the local and remote commits to match;
- reads `project.version` through `tools.release.project_version`;
- verifies that the corresponding `vX.Y.Z` tag matches that version; and
- creates the GitHub release at the verified commit with generated notes.

Creating the GitHub release starts `.github/workflows/release.yml` immediately.
That workflow rebuilds and verifies the distributions, pauses at the protected
`pypi` environment, and then publishes through PyPI Trusted Publishing after
the maintainer approves the deployment.

`tools/__init__.py` makes these Python helpers importable as `tools.release`
without adding them to the distributable `agent_orchestra` package.
