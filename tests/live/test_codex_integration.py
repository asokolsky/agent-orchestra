"""Opt-in end-to-end checks for the installed OpenAI Codex runtime."""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from agent_orchestra.adapter.codex import _developer_environment
from agent_orchestra.invocations import EffectiveModelStatus
from agent_orchestra.manifests import adapter_arguments
from agent_orchestra.runtime_metadata import reviewer_process_environment
from agent_orchestra.skill_install import skill_destination
from tests.live.runtime_harness import (
    LiveRuntime,
    assert_issue_scenario,
    assert_local_scenario,
    git,
    require_success,
    run_command,
    run_local_scenario,
)

if TYPE_CHECKING:
    from pathlib import Path

LIVE_CODEX = LiveRuntime(
    identifier='codex',
    executable='codex',
    opt_in_environment='AGENT_ORCHESTRA_LIVE_CODEX',
    model_environment='AGENT_ORCHESTRA_LIVE_CODEX_MODEL',
    skill_names=('agent-orchestra-reviewer', 'agent-orchestra-developer'),
    effective_model_status=EffectiveModelStatus.UNAVAILABLE,
)
LIVE_TIMEOUT_SECONDS = 300
SKIP_REASON = 'set AGENT_ORCHESTRA_LIVE_CODEX=1 to run live Codex checks'
SKILL_VERSION = re.compile(r'^\s+version:\s*"([^"]+)"\s*$', re.MULTILINE)

pytestmark = [
    pytest.mark.live_codex,
    pytest.mark.skipif(
        os.environ.get(LIVE_CODEX.opt_in_environment) != '1', reason=SKIP_REASON
    ),
]


def _installed_skill_version(skill: Path) -> str:
    """Return the installed skill version used as an invocation challenge."""

    match = SKILL_VERSION.search((skill / 'SKILL.md').read_text(encoding='utf-8'))
    if match is None:
        pytest.fail(f'installed Codex skill has no quoted metadata.version: {skill}')
    return match.group(1)


def _require_installed_skills() -> None:
    """Fail explicitly when either Codex source-code role skill is unavailable."""

    missing = [
        name
        for name in LIVE_CODEX.skill_names
        if not (skill_destination(LIVE_CODEX.identifier, name) / 'SKILL.md').is_file()
    ]
    if not missing:
        return
    commands = '; '.join(
        'agent-orchestra skills install --agent codex --skill ' + name
        for name in missing
    )
    pytest.fail(f'missing Codex role skills: {", ".join(missing)}; run {commands}')


def _probe_role_skill(
    executable: str,
    temporary: Path,
    *,
    role: str,
    skill_name: str,
    environment: dict[str, str],
) -> None:
    """Invoke one role skill with its production profile and structured output."""

    skill = skill_destination(LIVE_CODEX.identifier, skill_name)
    expected_version = _installed_skill_version(skill)
    schema_path = temporary / 'schema.json'
    result_path = temporary / 'result.json'
    schema_path.write_text(
        json.dumps(
            {
                'type': 'object',
                'properties': {
                    'skill_name': {'type': 'string'},
                    'skill_version': {'type': 'string'},
                },
                'required': ['skill_name', 'skill_version'],
                'additionalProperties': False,
            }
        ),
        encoding='utf-8',
    )
    command = [
        executable,
        *adapter_arguments(
            'codex',
            role,
            cwd=str(temporary),
            schema=str(schema_path),
            result=str(result_path),
        ),
        '--json',
    ]
    if LIVE_CODEX.requested_model is not None:
        command.extend(['--model', LIVE_CODEX.requested_model])
    command.append('-')
    completed = run_command(
        command,
        cwd=temporary,
        timeout=LIVE_TIMEOUT_SECONDS,
        environment=environment,
        input_text=(
            f'Invoke ${skill_name}. Do not run shell commands or read the skill file '
            'directly. Return the invoked skill name and its metadata.version.'
        ),
    )
    require_success(completed, context=f'Codex {skill_name} probe')
    try:
        events: tuple[Any, ...] = tuple(
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        )
        result: Any = json.loads(result_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as error:
        pytest.fail(f'Codex {skill_name} probe returned invalid JSON: {error}')
    assert any(
        isinstance(event, dict) and event.get('type') == 'turn.completed'
        for event in events
    )
    assert not any(
        isinstance(event, dict)
        and isinstance(event.get('item'), dict)
        and event['item'].get('type') == 'command_execution'
        for event in events
    )
    if not isinstance(result, dict) or result != {
        'skill_name': skill_name,
        'skill_version': expected_version,
    }:
        pytest.fail(
            f'Codex did not invoke ${skill_name}; reinstall the skill and confirm '
            'the installed CLI discovers user skills'
        )


@pytest.fixture(scope='module')
def codex_preflight(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Require an installed, authenticated CLI and two invokable role skills."""

    executable = shutil.which(LIVE_CODEX.executable)
    if executable is None:
        pytest.fail('codex executable not found on PATH')
    version = run_command([executable, '--version'], timeout=30)
    require_success(version, context='codex --version')
    auth = run_command([executable, 'login', 'status'], timeout=30)
    require_success(auth, context='codex login status')
    if 'logged in' not in (auth.stdout + auth.stderr).lower():
        pytest.fail('Codex authentication is unavailable; run `codex login`')
    _require_installed_skills()

    root = tmp_path_factory.mktemp('codex-preflight')
    for role, skill_name in (
        ('reviewer', 'agent-orchestra-reviewer'),
        ('developer', 'agent-orchestra-developer'),
    ):
        temporary = root / role
        temporary.mkdir()
        git(temporary, 'init', '--initial-branch=main')
        sandbox_temporary = root / f'{role}-sandbox'
        sandbox_temporary.mkdir()
        environments = {
            'reviewer': reviewer_process_environment(temporary),
            'developer': _developer_environment(temporary, sandbox_temporary),
        }
        _probe_role_skill(
            executable,
            temporary,
            role=role,
            skill_name=skill_name,
            environment=environments[role],
        )
    print(f'live Codex preflight: {version.stdout.strip()}')
    return executable


def test_live_codex_local_review_and_remediation(
    tmp_path: Path, codex_preflight: str
) -> None:
    """Find, remediate, and approve one deterministic temporary regression."""

    del codex_preflight
    scenario = run_local_scenario(
        tmp_path, LIVE_CODEX, attempt_timeout=LIVE_TIMEOUT_SECONDS
    )
    assert_local_scenario(scenario, LIVE_CODEX)


def test_live_codex_issue_review_uses_local_snapshot(
    tmp_path: Path,
    codex_preflight: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review deterministic issue prose without contacting or writing a provider."""

    del codex_preflight
    assert_issue_scenario(
        tmp_path,
        LIVE_CODEX,
        attempt_timeout=LIVE_TIMEOUT_SECONDS,
        monkeypatch=monkeypatch,
    )
