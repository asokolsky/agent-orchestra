"""Verify release tags and built Python distributions."""

from __future__ import annotations

import argparse
import email
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_NAME = 'py-agent-orchestra'
COMMAND_NAME = 'agent-orchestra'
PACKAGE_NAME = 'agent_orchestra'
SKILL_NAMES = ('agent-orchestra-developer', 'agent-orchestra-reviewer')
INLINE_MARKDOWN_LINK_DESTINATION = re.compile(r'!?\[[^]]*\]\(\s*<?([^\s)>]+)>?')
REFERENCE_MARKDOWN_LINK_DESTINATION = re.compile(
    r'^ {0,3}\[[^]]+\]:\s*<?([^\s>]+)>?', re.MULTILINE
)
FENCED_CODE_START = re.compile(r'^ {0,3}(`{3,}|~{3,})')
INLINE_CODE_SPAN = re.compile(r'(`+)(.*?)\1', re.DOTALL)


class ReleaseVerificationError(RuntimeError):
    """Report a release artifact that does not satisfy the package contract."""


def project_version(pyproject: Path = Path('pyproject.toml')) -> str:
    """Read the static package version from ``pyproject.toml``."""

    document = tomllib.loads(pyproject.read_text())
    project = document.get('project')
    if not isinstance(project, dict):
        message = 'pyproject.toml has no [project] table'
        raise ReleaseVerificationError(message)
    version = project.get('version')
    if not isinstance(version, str) or not version:
        message = 'project.version must be a non-empty string'
        raise ReleaseVerificationError(message)
    return version


def check_release_tag(tag: str, pyproject: Path = Path('pyproject.toml')) -> None:
    """Require a release tag to equal ``v`` plus the package version."""

    expected = f'v{project_version(pyproject)}'
    if tag != expected:
        raise ReleaseVerificationError(
            f'release tag {tag!r} does not match package version tag {expected!r}'
        )


def _one_archive(directory: Path, pattern: str, label: str) -> Path:
    """Return the only matching distribution archive."""

    matches = tuple(sorted(directory.glob(pattern)))
    if len(matches) != 1:
        raise ReleaseVerificationError(
            f'expected exactly one {label} in {directory}, found {len(matches)}'
        )
    return matches[0]


def distribution_archives(directory: Path) -> tuple[Path, Path]:
    """Return the single wheel and source distribution in a directory."""

    return (
        _one_archive(directory, '*.whl', 'wheel'),
        _one_archive(directory, '*.tar.gz', 'source distribution'),
    )


def _required_manifest_suffixes() -> tuple[str, ...]:
    """Return packaged manifest paths relative to the import package."""

    return tuple(
        f'{PACKAGE_NAME}/manifest/{path.name}'
        for path in sorted(Path(f'src/{PACKAGE_NAME}/manifest').glob('*.toml'))
    )


def _required_skill_suffixes(*, installed: bool = True) -> tuple[str, ...]:
    """Return archive paths for every canonical role skill file."""

    prefix = f'share/{COMMAND_NAME}/skills' if installed else 'skills'
    return tuple(
        f'{prefix}/{skill}/{filename}'
        for skill in SKILL_NAMES
        for filename in ('SKILL.md', 'SKILL-meta.md')
    )


def _require_suffixes(
    members: set[str], suffixes: tuple[str, ...], archive: Path
) -> None:
    """Require every relative path suffix to occur once in an archive."""

    for suffix in suffixes:
        matches = [member for member in members if member.endswith(suffix)]
        if len(matches) != 1:
            raise ReleaseVerificationError(
                f'{archive.name} must contain one {suffix}, found {len(matches)}'
            )


def _metadata_values(raw: bytes, archive: Path) -> tuple[str, str, str, set[str]]:
    """Extract release fields from core package metadata."""

    metadata = email.message_from_bytes(raw)
    name = metadata.get('Name')
    version = metadata.get('Version')
    python = metadata.get('Requires-Python')
    urls = set(metadata.get_all('Project-URL', []))
    if not all(isinstance(value, str) for value in (name, version, python)):
        raise ReleaseVerificationError(f'{archive.name} has incomplete core metadata')
    return str(name), str(version), str(python), urls


def _without_markdown_code(description: str) -> str:
    """Remove code blocks and spans whose link-like text is not rendered."""

    visible: list[str] = []
    fence: tuple[str, int] | None = None
    for line in description.splitlines(keepends=True):
        stripped = line.lstrip(' ')
        indentation = len(line) - len(stripped)
        if fence is not None:
            marker = stripped.rstrip('\r\n')
            character, minimum_length = fence
            run_length = len(marker) - len(marker.lstrip(character))
            if (
                indentation <= 3
                and run_length >= minimum_length
                and not marker[run_length:].strip()
            ):
                fence = None
            visible.append('\n' if line.endswith(('\n', '\r')) else '')
            continue
        match = FENCED_CODE_START.match(line)
        if match is not None:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            visible.append('\n' if line.endswith(('\n', '\r')) else '')
            continue
        if line.startswith(('    ', '\t')):
            visible.append('\n' if line.endswith(('\n', '\r')) else '')
            continue
        visible.append(line)
    return INLINE_CODE_SPAN.sub('', ''.join(visible))


def _check_description(raw: bytes, archive: Path) -> None:
    """Reject Markdown metadata with link targets that depend on its host URL."""

    metadata = email.message_from_bytes(raw)
    content_type = metadata.get('Description-Content-Type')
    description = metadata.get_payload()
    if (
        not isinstance(content_type, str)
        or content_type.partition(';')[0].strip().lower() != 'text/markdown'
        or not isinstance(description, str)
    ):
        raise ReleaseVerificationError(
            f'{archive.name} must contain a Markdown package description'
        )
    visible_description = _without_markdown_code(description)
    destinations = INLINE_MARKDOWN_LINK_DESTINATION.findall(
        visible_description
    ) + REFERENCE_MARKDOWN_LINK_DESTINATION.findall(visible_description)
    relative = sorted(
        {
            destination
            for destination in destinations
            if not destination.startswith('#') and not urlsplit(destination).scheme
        }
    )
    if relative:
        raise ReleaseVerificationError(
            f'{archive.name} description has relative link targets: {relative}'
        )


def _check_metadata(raw: bytes, archive: Path, expected_version: str) -> None:
    """Verify identity, version, Python support, and published project URLs."""

    name, version, python, urls = _metadata_values(raw, archive)
    _check_description(raw, archive)
    if name != PROJECT_NAME or version != expected_version or python != '>=3.14':
        raise ReleaseVerificationError(
            f'{archive.name} metadata is {name} {version} Python {python}'
        )
    url_labels = {entry.partition(',')[0].strip() for entry in urls}
    expected_labels = {'Homepage', 'Documentation', 'Issues', 'Repository'}
    if url_labels != expected_labels:
        raise ReleaseVerificationError(
            f'{archive.name} project URL labels are {sorted(url_labels)}'
        )


def check_distributions(directory: Path) -> tuple[Path, Path]:
    """Verify both archives contain matching metadata and canonical data."""

    wheel, source = distribution_archives(directory)
    expected_version = project_version()
    manifest_suffixes = _required_manifest_suffixes()
    installed_skill_suffixes = _required_skill_suffixes()

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        metadata_names = [
            name for name in members if name.endswith('.dist-info/METADATA')
        ]
        if len(metadata_names) != 1:
            raise ReleaseVerificationError(
                f'{wheel.name} must contain one METADATA file'
            )
        _check_metadata(archive.read(metadata_names[0]), wheel, expected_version)
        _require_suffixes(members, manifest_suffixes + installed_skill_suffixes, wheel)

    with tarfile.open(source, mode='r:gz') as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
        metadata_names = [
            name
            for name in members
            if name.endswith('/PKG-INFO') and name.count('/') == 1
        ]
        if len(metadata_names) != 1:
            raise ReleaseVerificationError(
                f'{source.name} must contain one PKG-INFO file'
            )
        metadata_file = archive.extractfile(metadata_names[0])
        if metadata_file is None:
            raise ReleaseVerificationError(f'cannot read {metadata_names[0]}')
        _check_metadata(metadata_file.read(), source, expected_version)
        _require_suffixes(
            members,
            manifest_suffixes + _required_skill_suffixes(installed=False),
            source,
        )

    return wheel, source


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one smoke-test command and preserve its diagnostic output."""

    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def smoke_test_wheel(directory: Path) -> None:
    """Install the wheel outside the checkout and exercise packaged data."""

    wheel, _ = check_distributions(directory)
    expected_version = project_version()
    with tempfile.TemporaryDirectory(prefix='agent-orchestra-wheel-') as temporary:
        root = Path(temporary)
        environment = root / 'venv'
        _run(
            ['uv', 'venv', '--python', sys.executable, str(environment)],
            root,
        )
        python = environment / 'bin/python'
        executable = environment / 'bin/agent-orchestra'
        _run(
            ['uv', 'pip', 'install', '--python', str(python), str(wheel.resolve())],
            root,
        )
        reported = _run([str(executable), '--version'], root).stdout.strip()
        if reported != f'{COMMAND_NAME} {expected_version}':
            raise ReleaseVerificationError(f'unexpected --version output: {reported!r}')

        _run(
            [
                str(python),
                '-c',
                (
                    'from agent_orchestra.manifests import '
                    'validate_packaged_manifests; validate_packaged_manifests()'
                ),
            ],
            root,
        )
        codex_home = root / 'codex'
        claude_home = root / 'claude'
        _run(
            [
                str(executable),
                'skills',
                'install',
                '--agent',
                'all',
                '--skill',
                SKILL_NAMES[0],
                '--skill',
                SKILL_NAMES[1],
                '--skill-home',
                f'codex={codex_home}',
                '--skill-home',
                f'claude-code={claude_home}',
            ],
            root,
        )
        for home in (codex_home, claude_home):
            for suffix in _required_skill_suffixes():
                relative = suffix.removeprefix(f'share/{COMMAND_NAME}/skills/')
                if not (home / 'skills' / relative).is_file():
                    raise ReleaseVerificationError(
                        f'installed wheel did not install {relative} into {home}'
                    )


def build_parser() -> argparse.ArgumentParser:
    """Build the release-verification command parser."""

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    tag = commands.add_parser('check-tag')
    tag.add_argument('tag')
    for name in ('check-dist', 'smoke-wheel'):
        command = commands.add_parser(name)
        command.add_argument('directory', type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one release verification command."""

    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == 'check-tag':
            check_release_tag(arguments.tag)
        elif arguments.command == 'check-dist':
            check_distributions(arguments.directory)
        else:
            smoke_test_wheel(arguments.directory)
    except (OSError, ReleaseVerificationError, subprocess.CalledProcessError) as error:
        print(f'release verification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
