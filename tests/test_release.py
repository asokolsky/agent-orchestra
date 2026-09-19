"""Tests for release tag and distribution verification."""

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import release
from tools.release import (
    ReleaseVerificationError,
    check_distributions,
    check_release_tag,
    project_version,
)

MANIFEST_NAMES = (
    'assignments.toml',
    'claude-code.toml',
    'codex.toml',
    'evidence.toml',
    'github.toml',
    'gitlab.toml',
)
SKILL_NAMES = ('agent-orchestra-developer', 'agent-orchestra-reviewer')
FIXTURE_VERSION = project_version()
PYPI_PUBLISH_COMMIT = 'dc37677b2e1c63e2034f94d8a5b11f265b73ba33'
PYPI_PUBLISH_TAG_OBJECT = 'a892a5a61159132606e93a2fa6f4358831b04d26'


def write_pyproject(path: Path, version: str) -> Path:
    """Write the smallest project table accepted by the release helper."""

    pyproject = path / 'pyproject.toml'
    pyproject.write_text(
        f'[project]\nname = "py-agent-orchestra"\nversion = "{version}"\n'
    )
    return pyproject


def core_metadata(version: str) -> bytes:
    """Return the core metadata fields enforced by the verifier."""

    return (
        'Metadata-Version: 2.4\n'
        'Name: py-agent-orchestra\n'
        f'Version: {version}\n'
        'Requires-Python: >=3.14\n'
        'Project-URL: Homepage, https://github.com/asokolsky/agent-orchestra\n'
        'Project-URL: Documentation, https://github.com/asokolsky/agent-orchestra/tree/main/docs\n'
        'Project-URL: Issues, https://github.com/asokolsky/agent-orchestra/issues\n'
        'Project-URL: Repository, https://github.com/asokolsky/agent-orchestra\n'
        '\n'
    ).encode()


def write_distributions(
    directory: Path,
    *,
    version: str = FIXTURE_VERSION,
    omitted_member: str | None = None,
) -> tuple[Path, Path]:
    """Build minimal wheel and source archives for verifier tests."""

    wheel = directory / f'py_agent_orchestra-{FIXTURE_VERSION}-py3-none-any.whl'
    wheel_members = {
        f'py_agent_orchestra-{FIXTURE_VERSION}.dist-info/METADATA': core_metadata(
            version
        ),
        **{
            f'agent_orchestra/manifest/{name}': b'manifest\n' for name in MANIFEST_NAMES
        },
        **{
            f'py_agent_orchestra-{FIXTURE_VERSION}.data/data/share/agent-orchestra/'
            f'skills/{skill}/{filename}': b'skill\n'
            for skill in SKILL_NAMES
            for filename in ('SKILL.md', 'SKILL-meta.md')
        },
    }
    with zipfile.ZipFile(wheel, mode='w') as archive:
        for name, content in wheel_members.items():
            if name != omitted_member:
                archive.writestr(name, content)

    source = directory / f'py_agent_orchestra-{FIXTURE_VERSION}.tar.gz'
    root = f'py_agent_orchestra-{FIXTURE_VERSION}'
    source_members = {
        f'{root}/PKG-INFO': core_metadata(version),
        **{
            f'{root}/src/agent_orchestra/manifest/{name}': b'manifest\n'
            for name in MANIFEST_NAMES
        },
        **{
            f'{root}/skills/{skill}/{filename}': b'skill\n'
            for skill in SKILL_NAMES
            for filename in ('SKILL.md', 'SKILL-meta.md')
        },
    }
    with tarfile.open(source, mode='w:gz') as archive:
        for name, content in source_members.items():
            if name == omitted_member:
                continue
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return wheel, source


def test_project_version_reads_static_metadata(tmp_path: Path) -> None:
    """Use pyproject.toml as the release version source of truth."""

    assert project_version(write_pyproject(tmp_path, '1.2.3')) == '1.2.3'


def test_release_tag_matches_project_version(tmp_path: Path) -> None:
    """Accept only a v-prefixed tag for the exact package version."""

    pyproject = write_pyproject(tmp_path, '1.2.3')
    check_release_tag('v1.2.3', pyproject)

    with pytest.raises(ReleaseVerificationError, match='does not match'):
        check_release_tag('v1.2.4', pyproject)


def test_release_workflow_pins_publisher_to_peeled_commit() -> None:
    """Use the publisher's GHCR-backed commit, not its annotated tag object."""

    workflow = Path('.github/workflows/release.yml').read_text()

    assert f'pypa/gh-action-pypi-publish@{PYPI_PUBLISH_COMMIT}' in workflow
    assert PYPI_PUBLISH_TAG_OBJECT not in workflow


def test_check_distributions_accepts_complete_archives(tmp_path: Path) -> None:
    """Accept matching metadata, manifests, and role skills in both archives."""

    wheel, source = write_distributions(tmp_path)

    assert check_distributions(tmp_path) == (wheel, source)


@pytest.mark.parametrize(
    ('version', 'omitted_member', 'message'),
    [
        ('9.9.9', None, 'metadata is py-agent-orchestra 9.9.9'),
        (
            FIXTURE_VERSION,
            'agent_orchestra/manifest/codex.toml',
            'must contain one agent_orchestra/manifest/codex.toml',
        ),
        (
            FIXTURE_VERSION,
            (
                f'py_agent_orchestra-{FIXTURE_VERSION}/skills/'
                'agent-orchestra-developer/SKILL.md'
            ),
            'must contain one skills/agent-orchestra-developer/SKILL.md',
        ),
    ],
)
def test_check_distributions_rejects_invalid_archives(
    tmp_path: Path,
    version: str,
    omitted_member: str | None,
    message: str,
) -> None:
    """Reject wrong versions and missing canonical package data."""

    write_distributions(tmp_path, version=version, omitted_member=omitted_member)

    with pytest.raises(ReleaseVerificationError, match=message):
        check_distributions(tmp_path)


def test_smoke_test_wheel_exercises_installed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise version, manifest, and skill checks through the installed CLI."""

    wheel = tmp_path / f'py_agent_orchestra-{FIXTURE_VERSION}-py3-none-any.whl'
    source = tmp_path / f'py_agent_orchestra-{FIXTURE_VERSION}.tar.gz'
    calls: list[list[str]] = []

    def fake_check_distributions(directory: Path) -> tuple[Path, Path]:
        """Return synthetic archives without repeating archive verification."""

        assert directory == tmp_path
        return wheel, source

    def fake_project_version() -> str:
        """Return the version expected from the synthetic installed CLI."""

        return FIXTURE_VERSION

    def fake_run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        """Record smoke commands and materialize the requested skill installs."""

        calls.append(command)
        if command[-1] == '--version':
            return subprocess.CompletedProcess(
                command, 0, stdout=f'agent-orchestra {FIXTURE_VERSION}\n', stderr=''
            )
        if 'skills' in command:
            for index, argument in enumerate(command):
                if argument != '--skill-home':
                    continue
                _, _, home = command[index + 1].partition('=')
                for skill in SKILL_NAMES:
                    for filename in ('SKILL.md', 'SKILL-meta.md'):
                        destination = Path(home) / 'skills' / skill / filename
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text('installed\n')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(release, 'check_distributions', fake_check_distributions)
    monkeypatch.setattr(release, 'project_version', fake_project_version)
    monkeypatch.setattr(release, '_run', fake_run)

    release.smoke_test_wheel(tmp_path)

    assert any(command[-1] == '--version' for command in calls)
    assert any('validate_packaged_manifests' in ' '.join(command) for command in calls)
    assert any('skills' in command for command in calls)
