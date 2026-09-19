# Releasing to PyPI

Releases begin with a GitHub release and end with the exact distributions that
passed the release checks being uploaded to PyPI. A branch push or pull request
can build and inspect those files, but it cannot publish them.

## One-time setup

The registered PyPI project is `py-agent-orchestra`. Add its Trusted Publisher
using these identity values:

- owner: `asokolsky`
- repository: `agent-orchestra`
- workflow: `release.yml`
- environment: `pypi`

The [PyPI Trusted Publisher setup][pypi-trusted-publisher] explains both the
pending-project and existing-project paths.

Create a GitHub environment named `pypi`. Add a required maintainer review and
allow only release tags matching `v*`. PyPI recommends an environment because
its protection rules add a separate approval boundary around the OIDC identity.
The workflow contains no PyPI password or long-lived upload token.

## Choose the version

`project.version` in `pyproject.toml` is the source of truth. The CLI reads the
installed distribution metadata, so a wheel built with version `X.Y.Z` reports
`agent-orchestra X.Y.Z`.

The Git tag must be that same version prefixed with `v`. For example,
`project.version = "0.2.0"` requires tag `v0.2.0`. The release workflow stops
before building if they differ.

## Prepare the release

Update `project.version`, then run the complete repo gate:

```shell
mise run format
mise run lint
mise run mypy
mise run tests
mise run build
mise run verify-dist
git diff --check
```

`verify-dist` checks the source distribution and wheel metadata, confirms that
every packaged manifest and canonical role skill is present, installs the wheel
in a temporary environment outside the checkout, and exercises the installed
CLI. It also installs both role skills for Codex and Claude Code from the wheel.

## Publish the release

After the version change is reviewed and merged, create a GitHub release for
the matching tag.

Clickops:

1. Open the [Releases page](https://github.com/asokolsky/agent-orchestra/releases)
   and click **Create a new release**.
2. Under **Choose a tag**, enter the matching tag such as `v0.2.0`, select
   **Create new tag**, and target the merged commit on `main`.
3. Use the tag as the release title, add or generate release notes, then select
   **Publish release**. Saving a draft does not start publication.

See [GitHub's release instructions][github-release] for screenshots of this
flow.

Alternatively, create the release from an up-to-date `main` checkout with:

```shell
VERSION="$(
  uv run python -c \
    'from tools.release import project_version; print(project_version())'
)"
mise run verify-release-tag -- "v$VERSION"
gh release create "v$VERSION" \
  --repo asokolsky/agent-orchestra \
  --target "$(git rev-parse HEAD)" \
  --title "v$VERSION" \
  --generate-notes
```

The first command reads `project.version` from `pyproject.toml`, the source of
truth used by the release checks. `gh release create` publishes immediately.

Both clickops and the CLI start `.github/workflows/release.yml`. The workflow
rebuilds from the released tag, repeats the complete gate and distribution
smoke test, and passes the verified files to a separate publish job.

The publish job receives only `id-token: write`. PyPI exchanges that GitHub OIDC
identity for a short-lived credential and rejects a version that already
exists. The workflow does not enable the publisher action's `skip-existing`
option, so a repeated release attempt cannot silently replace or ignore an
immutable release.

## Verify the release

After the job succeeds, verify both supported installation paths from outside
the source checkout:

```shell
uv tool install py-agent-orchestra
agent-orchestra --version
pipx install py-agent-orchestra
```

[github-release]: https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository#creating-a-release
[pypi-trusted-publisher]: https://docs.pypi.org/trusted-publishers/adding-a-publisher/
