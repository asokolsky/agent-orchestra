"""Build a fail-closed macOS process boundary for OpenCode role invocations."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_orchestra.adapter.errors import AdapterError
from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.runtime_metadata import RUNTIME_METADATA_ENV

OPENCODE_SUPPORTED_VERSION = '1.18.33'


class OpenCodeIsolationError(AdapterError):
    """Report that an OpenCode invocation cannot meet its isolation contract."""


def isolated_environment(
    scratch: Path,
    directory: Path,
    role: RuntimeRole = RuntimeRole.ISSUE_REVIEWER,
    *,
    external_read_paths: tuple[Path, ...] = (),
    review_base_sha: str | None = None,
) -> dict[str, str]:
    """
    Hide inherited OpenCode and project settings while retaining provider keys.

    The caller must create and own ``scratch`` outside the target worktree.
    ``directory`` is the CLI's current directory. Both paths must be absolute.
    """

    if not isinstance(role, RuntimeRole) or role not in {
        RuntimeRole.REVIEWER,
        RuntimeRole.DEVELOPER,
        RuntimeRole.ISSUE_REVIEWER,
    }:
        msg = f'unsupported OpenCode role: {role}'
        raise OpenCodeIsolationError(msg)
    scratch = scratch.resolve()
    directory = directory.resolve()
    if scratch.is_relative_to(directory) or directory.is_relative_to(scratch):
        msg = 'OpenCode scratch and worktree must be separate'
        raise OpenCodeIsolationError(msg)

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('OPENCODE_', 'XDG_'))
        and key
        not in {
            'NODE_OPTIONS',
            'BUN_CONFIG',
            'BUN_CONFIG_FILE',
            RUNTIME_METADATA_ENV,
        }
    }
    locations = {
        'HOME': scratch / 'home',
        'PWD': directory,
        'TMPDIR': scratch / 'tmp',
        'XDG_CONFIG_HOME': scratch / 'config',
        'XDG_DATA_HOME': scratch / 'data',
        'XDG_CACHE_HOME': scratch / 'cache',
        'XDG_STATE_HOME': scratch / 'state',
    }
    for key, path in locations.items():
        if key != 'PWD':
            path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    config_directory = scratch / 'config' / 'opencode'
    config_directory.mkdir(parents=True, exist_ok=True)
    gitignore = config_directory / '.gitignore'
    if not gitignore.exists():
        gitignore.write_text('node_modules\npackage.json\npackage-lock.json\n')
    data_home = Path(
        os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))
    ).expanduser()
    auth_source = data_home / 'opencode' / 'auth.json'
    if auth_source.is_symlink():
        msg = 'OpenCode auth file must not be a symlink'
        raise OpenCodeIsolationError(msg)
    if auth_source.is_file():
        auth_target = scratch / 'data' / 'opencode' / 'auth.json'
        auth_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(auth_source, auth_target)
        auth_target.chmod(0o600)
    permissions: dict[str, Any] = {'*': 'deny'}
    if role is RuntimeRole.REVIEWER:
        if (
            review_base_sha is None
            or re.fullmatch(r'[0-9a-f]{40}', review_base_sha) is None
        ):
            msg = 'OpenCode reviewer requires a full hexadecimal base SHA'
            raise OpenCodeIsolationError(msg)
        permissions.update(
            read='allow',
            glob='allow',
            grep='allow',
            skill={'*': 'deny', 'agent-orchestra-reviewer': 'allow'},
            bash={
                '*': 'deny',
                'git status --short': 'allow',
                'git rev-parse HEAD': 'allow',
                'git ls-files --others --exclude-standard': 'allow',
                f'git diff --no-ext-diff --binary {review_base_sha}': 'allow',
            },
        )
    elif role is RuntimeRole.DEVELOPER:
        permissions.update(
            read='allow',
            glob='allow',
            grep='allow',
            edit='allow',
            bash='allow',
            skill={'*': 'deny', 'agent-orchestra-developer': 'allow'},
        )
        parent_mise_data = Path(
            os.environ.get('MISE_DATA_DIR', str(Path.home() / '.local/share/mise'))
        ).expanduser()
        shared_installs = [
            str(parent_mise_data / 'installs'),
            *os.environ.get('MISE_SHARED_INSTALL_DIRS', '').split(os.pathsep),
        ]
        environment.update(
            MISE_CACHE_DIR=str(scratch / 'mise-cache'),
            MISE_DATA_DIR=str(scratch / 'mise-data'),
            MISE_INSTALLS_DIR=str(scratch / 'mise-data' / 'installs'),
            MISE_SHARED_INSTALL_DIRS=os.pathsep.join(
                dict.fromkeys(path for path in shared_installs if path)
            ),
            MISE_STATE_DIR=str(scratch / 'mise-state'),
            MISE_TRUSTED_CONFIG_PATHS=str(directory),
            UV_CACHE_DIR=str(scratch / 'uv-cache'),
            MYPY_CACHE_DIR=str(scratch / 'mypy-cache'),
            RUFF_CACHE_DIR=str(scratch / 'ruff-cache'),
            PYTHONDONTWRITEBYTECODE='1',
        )
    elif role is RuntimeRole.ISSUE_REVIEWER:
        # Issue review uses the default-deny tool policy.
        pass
    if external_read_paths:
        permissions['external_directory'] = {
            '*': 'deny',
            **{str(path.resolve()): 'allow' for path in external_read_paths},
        }
    environment.update(
        OPENCODE_DISABLE_PROJECT_CONFIG='1',
        OPENCODE_DISABLE_AUTOUPDATE='1',
        OPENCODE_CONFIG_CONTENT=json.dumps(
            {
                'share': 'disabled',
                'permission': permissions,
            },
            separators=(',', ':'),
        ),
    )
    return environment


def sandbox_command(
    executable: Path, scratch: Path, *, writable_worktree: Path | None = None
) -> list[str]:
    """
    Wrap OpenCode in a filesystem write boundary or reject this host.

    The caller must keep ``scratch`` outside the worktree. This rejects systems
    without the macOS sandbox rather than silently dropping containment.
    """

    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        msg = 'OpenCode requires macOS sandbox-exec'
        raise OpenCodeIsolationError(msg)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        msg = 'OpenCode executable is unavailable'
        raise OpenCodeIsolationError(msg)
    scratch = scratch.resolve()
    paths = [scratch]
    if writable_worktree is not None:
        worktree = writable_worktree.resolve()
        if scratch.is_relative_to(worktree) or worktree.is_relative_to(scratch):
            msg = 'OpenCode scratch and worktree must be separate'
            raise OpenCodeIsolationError(msg)
        paths.append(worktree)
    profile = '\n'.join(
        [
            '(version 1)',
            '(allow default)',
            '(deny file-write*)',
            '(allow file-write* '
            + ' '.join(
                f'(literal {json.dumps(path)})'
                for path in (
                    '/dev/null',
                    '/dev/tty',
                    '/dev/stdout',
                    '/dev/stderr',
                    '/dev/dtracehelper',
                )
            )
            + ')',
            *(
                f'(allow file-write* (subpath {json.dumps(str(path))}))'
                for path in paths
            ),
            f'(deny file-write* (subpath {json.dumps(str(scratch / "config/opencode"))}))',
            '',
        ]
    )
    return ['/usr/bin/sandbox-exec', '-p', profile, str(executable)]


def require_opencode() -> Path:
    """Find the installed CLI without invoking a shell alias or search path later."""

    located = shutil.which('opencode')
    if located is None:
        msg = 'opencode executable not found'
        raise OpenCodeIsolationError(msg)
    return Path(located).resolve()


def require_supported_version(executable: Path, scratch: Path, directory: Path) -> None:
    """
    Accept only the installed release whose CLI and isolation were probed.

    The version check itself runs inside the same operating-system boundary.
    Any unexpected output, failure, or timeout leaves the role unavailable.
    """

    command = [*sandbox_command(executable, scratch), '--version']
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=directory,
            env=isolated_environment(scratch, directory),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        msg = 'OpenCode version check failed'
        raise OpenCodeIsolationError(msg) from error
    if result.returncode != 0 or result.stdout.strip() != OPENCODE_SUPPORTED_VERSION:
        raise OpenCodeIsolationError(
            f'OpenCode release is unsupported; required {OPENCODE_SUPPORTED_VERSION}'
        )
