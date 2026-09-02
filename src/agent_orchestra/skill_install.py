"""Install bundled agent skills without a Node.js dependency."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sysconfig
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

INSTALL_MANIFEST = '.agent-orchestra-install.json'
HASH_CHUNK_SIZE = 1024 * 1024


class SkillInstallError(RuntimeError):
    """Raised when a skill installation cannot be completed safely."""


class AgentTarget(StrEnum):
    """Agent runtimes supported by the skill installer."""

    CODEX = 'codex'
    CLAUDE_CODE = 'claude-code'


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of installing one skill for one agent runtime."""

    agent: AgentTarget
    skill: str
    destination: Path
    installed: bool


def find_skills_root(explicit_source: Path | None = None) -> Path:
    """Locate canonical skills in a checkout or installed distribution."""

    if explicit_source is not None:
        source = explicit_source.expanduser().resolve()
        if source.is_dir():
            return source
        message = f'skill source directory not found: {source}'
        raise SkillInstallError(message)

    checkout_source = Path(__file__).resolve().parents[2] / 'skills'
    installed_source = (
        Path(sysconfig.get_path('data')) / 'share' / 'agent-orchestra' / 'skills'
    )
    for candidate in (checkout_source, installed_source):
        if candidate.is_dir():
            return candidate

    message = 'bundled skills directory not found; use --source to provide one'
    raise SkillInstallError(message)


def install_skills(
    skill_names: tuple[str, ...],
    agents: tuple[AgentTarget, ...],
    *,
    source_root: Path | None = None,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
) -> tuple[InstallResult, ...]:
    """Install each skill atomically after validating every destination."""

    root = find_skills_root(source_root)
    sources = {name: _validate_source(root, name) for name in skill_names}
    destinations = [
        (agent, name, _destination(agent, name, codex_home, claude_home))
        for agent in agents
        for name in skill_names
    ]

    source_digests = {
        name: _directory_digest(source) for name, source in sources.items()
    }
    destination_digests: dict[Path, str] = {}
    for agent, name, destination in destinations:
        if not destination.exists():
            continue
        destination_digest = _directory_digest(destination)
        destination_digests[destination] = destination_digest
        if destination_digest == source_digests[name]:
            continue
        installed_digest = _installed_source_digest(destination)
        if installed_digest is None or destination_digest != installed_digest:
            message = (
                f'refusing to overwrite locally modified {agent} skill at {destination}'
            )
            raise SkillInstallError(message)

    results: list[InstallResult] = []
    for agent, name, destination in destinations:
        if destination_digests.get(destination) == source_digests[name]:
            results.append(InstallResult(agent, name, destination, installed=False))
            continue
        _copy_atomically(sources[name], destination, source_digests[name])
        results.append(InstallResult(agent, name, destination, installed=True))
    return tuple(results)


def _validate_source(root: Path, skill_name: str) -> Path:
    """Return a safe source directory for a requested skill."""

    if not skill_name or Path(skill_name).name != skill_name:
        message = f'invalid skill name: {skill_name!r}'
        raise SkillInstallError(message)
    source = root / skill_name
    if not source.is_dir() or not (source / 'SKILL.md').is_file():
        message = f'skill not found: {skill_name}'
        raise SkillInstallError(message)
    for path in source.rglob('*'):
        if path.is_symlink():
            message = f'skill source contains unsupported symlink: {path}'
            raise SkillInstallError(message)
    return source


def _destination(
    agent: AgentTarget,
    skill_name: str,
    codex_home: Path | None,
    claude_home: Path | None,
) -> Path:
    """Resolve the personal skill destination for an agent runtime."""

    if agent is AgentTarget.CODEX:
        configured = codex_home
        if configured is None:
            codex_home_env = os.environ.get('CODEX_HOME')
            configured = (
                Path(codex_home_env) if codex_home_env else Path.home() / '.codex'
            )
    else:
        configured = claude_home
        if configured is None:
            claude_config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
            configured = (
                Path(claude_config_dir)
                if claude_config_dir
                else Path.home() / '.claude'
            )
    return configured.expanduser().resolve() / 'skills' / skill_name


def _directory_digest(directory: Path) -> str:
    """Return a deterministic digest of regular files in a skill directory."""

    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            message = f'skill directory contains unsupported symlink: {path}'
            raise SkillInstallError(message)
        if path.is_dir():
            continue
        if path.name == INSTALL_MANIFEST and path.parent == directory:
            continue
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b'\0')
        digest.update((path.stat().st_mode & 0o111).to_bytes(2))
        with path.open('rb') as file:
            while chunk := file.read(HASH_CHUNK_SIZE):
                digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()


def _installed_source_digest(destination: Path) -> str | None:
    """Return the source digest recorded by a prior managed installation."""

    manifest = destination / INSTALL_MANIFEST
    if not manifest.is_file():
        return None
    try:
        document = json.loads(manifest.read_text(encoding='utf-8'))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(document, dict) or document.get('schemaVersion') != 1:
        return None
    digest = document.get('sourceDigest')
    return digest if isinstance(digest, str) else None


def _copy_atomically(source: Path, destination: Path, source_digest: str) -> None:
    """Copy a skill into place without exposing a partial installation."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f'.{destination.name}.', dir=destination.parent)
    )
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        (temporary / INSTALL_MANIFEST).write_text(
            json.dumps({'schemaVersion': 1, 'sourceDigest': source_digest}) + '\n',
            encoding='utf-8',
        )
        if destination.exists():
            backup_parent = Path(
                tempfile.mkdtemp(
                    prefix=f'.{destination.name}.backup.', dir=destination.parent
                )
            )
            backup = backup_parent / destination.name
            destination.replace(backup)
            try:
                temporary.replace(destination)
            except Exception:
                backup.replace(destination)
                raise
            shutil.rmtree(backup_parent, ignore_errors=True)
        else:
            temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
