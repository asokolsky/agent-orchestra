"""Verify OpenCode's isolated configuration and operating-system boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

from agent_orchestra.adapter.opencode import _stage_skill
from agent_orchestra.adapter.opencode_isolation import (
    OpenCodeIsolationError,
    isolated_environment,
    require_opencode,
    sandbox_command,
)


def test_isolated_environment_hides_inherited_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global, project, and injected config cannot enter a role invocation."""

    monkeypatch.setenv('OPENCODE_CONFIG_DIR', '/untrusted/config')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"permission":{"*":"allow"}}')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/untrusted/xdg')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'original-data'))
    monkeypatch.setenv('NODE_OPTIONS', '--require /untrusted/plugin.js')
    monkeypatch.setenv('AGENT_ORCHESTRA_RUNTIME_METADATA_PATH', '/untrusted/sidecar')
    monkeypatch.setenv('INFERENCE_API_KEY', 'test-only-key')
    scratch = tmp_path / 'scratch'
    worktree = tmp_path / 'worktree'
    worktree.mkdir()

    environment = isolated_environment(scratch, worktree)

    assert environment['HOME'] == str(scratch / 'home')
    assert environment['PWD'] == str(worktree)
    assert environment['XDG_CONFIG_HOME'] == str(scratch / 'config')
    assert environment['INFERENCE_API_KEY'] == 'test-only-key'
    assert environment['OPENCODE_DISABLE_PROJECT_CONFIG'] == '1'
    assert environment['OPENCODE_DISABLE_AUTOUPDATE'] == '1'
    assert json.loads(environment['OPENCODE_CONFIG_CONTENT']) == {
        'share': 'disabled',
        'permission': {'*': 'deny'},
    }
    assert 'OPENCODE_CONFIG_DIR' not in environment
    assert 'NODE_OPTIONS' not in environment
    assert 'AGENT_ORCHESTRA_RUNTIME_METADATA_PATH' not in environment


@pytest.mark.parametrize(
    ('role', 'skill_name'),
    [
        ('reviewer', 'agent-orchestra-reviewer'),
        ('developer', 'agent-orchestra-developer'),
    ],
)
def test_role_permissions_allow_only_the_staged_skill(
    tmp_path: Path, role: Literal['reviewer', 'developer'], skill_name: str
) -> None:
    """A role can load its own skill while unrelated skills remain denied."""

    scratch = tmp_path / 'scratch'
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    review = tmp_path / 'runs' / 'review.json'
    base_sha = 'a' * 40
    environment = isolated_environment(
        scratch,
        worktree,
        role,
        external_read_paths=(review,),
        review_base_sha=base_sha if role == 'reviewer' else None,
    )
    permission = json.loads(environment['OPENCODE_CONFIG_CONTENT'])['permission']

    assert permission['skill'] == {'*': 'deny', skill_name: 'allow'}
    assert permission['external_directory'] == {
        '*': 'deny',
        str(review): 'allow',
    }
    if role == 'reviewer':
        assert permission['bash'] == {
            '*': 'deny',
            'git status --short': 'allow',
            'git rev-parse HEAD': 'allow',
            'git ls-files --others --exclude-standard': 'allow',
            f'git diff --no-ext-diff --binary {base_sha}': 'allow',
        }


def test_only_opencode_auth_is_copied_into_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain provider login without loading the user's OpenCode config."""

    original = tmp_path / 'original-data' / 'opencode'
    original.mkdir(parents=True)
    (original / 'auth.json').write_text('{"provider":"test"}\n')
    (original / 'opencode.json').write_text('{"permission":{"*":"allow"}}\n')
    monkeypatch.setenv('XDG_DATA_HOME', str(original.parent))
    scratch = tmp_path / 'scratch'
    worktree = tmp_path / 'worktree'
    worktree.mkdir()

    isolated_environment(scratch, worktree)

    assert (scratch / 'data/opencode/auth.json').read_text() == (
        '{"provider":"test"}\n'
    )
    assert not (scratch / 'data/opencode/opencode.json').exists()


def test_isolated_environment_rejects_nested_scratch(tmp_path: Path) -> None:
    """Run state may not be placed in the target worktree."""

    with pytest.raises(OpenCodeIsolationError, match='must be separate'):
        isolated_environment(tmp_path / 'repo' / 'scratch', tmp_path / 'repo')


@pytest.mark.skipif(
    sys.platform != 'darwin' or os.environ.get('AGENT_ORCHESTRA_LIVE_OPENCODE') != '1',
    reason='opt-in macOS sandbox check',
)
def test_sandbox_rejects_external_write(tmp_path: Path) -> None:
    """The OS permits scratch writes and denies another directory's writes."""

    scratch = tmp_path / 'scratch'
    outside = tmp_path / 'outside'
    scratch.mkdir()
    outside.mkdir()
    config = scratch / 'config/opencode'
    config.mkdir(parents=True)
    touch = Path('/usr/bin/touch')
    command = sandbox_command(touch, scratch)

    inside_result = subprocess.run([*command, str(scratch / 'allowed')], check=False)
    null_result = subprocess.run(
        [*sandbox_command(Path('/bin/sh'), scratch), '-c', 'echo x > /dev/null'],
        check=False,
    )
    outside_result = subprocess.run([*command, str(outside / 'denied')], check=False)
    config_result = subprocess.run([*command, str(config / 'denied')], check=False)

    assert inside_result.returncode == 0
    assert (scratch / 'allowed').exists()
    assert null_result.returncode == 0
    assert outside_result.returncode != 0
    assert not (outside / 'denied').exists()
    assert config_result.returncode != 0
    assert not (config / 'denied').exists()


@pytest.mark.skipif(
    sys.platform != 'darwin' or os.environ.get('AGENT_ORCHESTRA_LIVE_OPENCODE') != '1',
    reason='opt-in macOS linked-worktree sandbox check',
)
def test_reviewer_git_commands_work_in_linked_worktree(tmp_path: Path) -> None:
    """Read-only Git can inspect tracked and untracked changes across a gitdir link."""

    source = tmp_path / 'source'
    linked = tmp_path / 'linked'
    scratch = tmp_path / 'scratch'
    git = Path(shutil.which('git') or '/usr/bin/git').resolve()
    source.mkdir()
    scratch.mkdir()
    subprocess.run([str(git), 'init', '-q', str(source)], check=True)
    (source / 'tracked.txt').write_text('before\n')
    subprocess.run([str(git), 'add', 'tracked.txt'], cwd=source, check=True)
    subprocess.run(
        [
            str(git),
            '-c',
            'user.name=Test',
            '-c',
            'user.email=test@example.com',
            'commit',
            '-qm',
            'base',
        ],
        cwd=source,
        check=True,
    )
    base_sha = subprocess.check_output(
        [str(git), 'rev-parse', 'HEAD'], cwd=source, text=True
    ).strip()
    subprocess.run(
        [str(git), 'worktree', 'add', '-qb', 'review', str(linked), base_sha],
        cwd=source,
        check=True,
    )
    (linked / 'tracked.txt').write_text('after\n')
    (linked / 'untracked.txt').write_text('new\n')
    command = sandbox_command(git, scratch)
    status = subprocess.run(
        [*command, 'status', '--short'],
        cwd=linked,
        capture_output=True,
        text=True,
        check=False,
    )
    head = subprocess.run(
        [*command, 'rev-parse', 'HEAD'],
        cwd=linked,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = subprocess.run(
        [*command, 'diff', '--no-ext-diff', '--binary', base_sha],
        cwd=linked,
        capture_output=True,
        text=True,
        check=False,
    )
    untracked = subprocess.run(
        [*command, 'ls-files', '--others', '--exclude-standard'],
        cwd=linked,
        capture_output=True,
        text=True,
        check=False,
    )

    assert status.returncode == 0, status.stderr
    assert ' M tracked.txt' in status.stdout
    assert '?? untracked.txt' in status.stdout
    assert head.returncode == 0, head.stderr
    assert head.stdout.strip() == base_sha
    assert changed.returncode == 0, changed.stderr
    assert '+after' in changed.stdout
    assert untracked.returncode == 0, untracked.stderr
    assert untracked.stdout.strip() == 'untracked.txt'


@pytest.mark.skipif(
    sys.platform != 'darwin' or os.environ.get('AGENT_ORCHESTRA_LIVE_OPENCODE') != '1',
    reason='opt-in OpenCode config isolation check',
)
def test_live_opencode_ignores_project_agent_and_denies_tools(tmp_path: Path) -> None:
    """The installed release must honor the role config isolation flags."""

    repo = tmp_path / 'repo'
    scratch = tmp_path / 'scratch'
    repo.mkdir()
    scratch.mkdir()
    (repo / 'opencode.json').write_text(
        '{"agent":{"rogue":{"description":"project agent","prompt":"test"}}}'
    )
    _stage_skill('agent-orchestra-reviewer', scratch)
    result = subprocess.run(
        [*sandbox_command(require_opencode(), scratch), 'agent', 'list', '--pure'],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=repo,
        env=isolated_environment(scratch, repo, 'reviewer', review_base_sha='a' * 40),
    )

    assert result.returncode == 0
    assert 'rogue' not in result.stdout
    build = result.stdout.split('build (primary)', 1)[1]
    rules, _ = json.JSONDecoder().raw_decode(build.lstrip())
    assert isinstance(rules, list)
    assert any(
        rule.get('permission') == '*'
        and rule.get('action') == 'deny'
        and rule.get('pattern') == '*'
        for rule in rules
        if isinstance(rule, dict)
    )
    skills = subprocess.run(
        [
            *sandbox_command(require_opencode(), scratch),
            'debug',
            'skill',
            '--pure',
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=repo,
        env=isolated_environment(scratch, repo, 'reviewer', review_base_sha='a' * 40),
    )
    assert skills.returncode == 0
    assert 'agent-orchestra-reviewer' in skills.stdout
    assert any(
        rule.get('permission') == 'skill'
        and rule.get('action') == 'allow'
        and rule.get('pattern') == 'agent-orchestra-reviewer'
        for rule in rules
        if isinstance(rule, dict)
    )
