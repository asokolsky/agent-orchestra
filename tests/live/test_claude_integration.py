"""Opt-in end-to-end checks for the installed Claude Code runtime."""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from agent_orchestra.adapter.claude_code import (
    CLAUDE_DEVELOPER_SKILL,
    CLAUDE_REVIEWER_SKILL,
    _developer_settings,
    _stage_skill,
)
from agent_orchestra.runtime_metadata import child_process_environment
from agent_orchestra.skill_install import skill_destination
from tests.live.runtime_harness import (
    LiveRuntime,
    assert_issue_scenario,
    assert_local_scenario,
    assert_runtime_capabilities,
    live_runtime,
    require_success,
    run_command,
    run_local_scenario,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

LIVE_CLAUDE: LiveRuntime = live_runtime('claude-code')
LIVE_TIMEOUT_SECONDS = 240
SKIP_REASON = 'set AGENT_ORCHESTRA_LIVE_CLAUDE=1 to run live Claude checks'

pytestmark = [
    pytest.mark.live_claude,
    pytest.mark.skipif(
        os.environ.get(LIVE_CLAUDE.opt_in_environment) != '1', reason=SKIP_REASON
    ),
]


def _advertises_skill(event: object, skill_name: str) -> bool:
    """Return whether Claude initialized with the expected invokable skill."""

    if not isinstance(event, dict):
        return False
    if event.get('type') != 'system' or event.get('subtype') != 'init':
        return False
    skills = event.get('skills')
    commands = event.get('slash_commands')
    return (
        isinstance(skills, list)
        and skill_name in skills
        and isinstance(commands, list)
        and skill_name in commands
    )


def _require_installed_skills() -> None:
    """Fail explicitly when either source-code role skill is unavailable."""

    missing = [
        name
        for name in LIVE_CLAUDE.skill_names
        if not (skill_destination(LIVE_CLAUDE.identifier, name) / 'SKILL.md').is_file()
    ]
    if not missing:
        return
    commands = '; '.join(
        'agent-orchestra skills install --agent claude-code --skill ' + name
        for name in missing
    )
    pytest.fail(f'missing Claude role skills: {", ".join(missing)}; run {commands}')


def _probe_role_skill(
    executable: str,
    temporary: Path,
    *,
    installed_name: str,
    invokable_name: str,
) -> None:
    """Observe Claude invoke one installed role skill rather than infer it."""

    plugin = _stage_skill(
        skill_destination(LIVE_CLAUDE.identifier, installed_name),
        temporary,
    )
    command = [
        executable,
        '--print',
        '--no-session-persistence',
        '--setting-sources',
        '',
        '--settings',
        _developer_settings(),
        '--strict-mcp-config',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--output-format',
        'stream-json',
        '--verbose',
        '--permission-mode',
        'dontAsk',
        '--plugin-dir',
        str(plugin),
        '--tools',
        'Skill',
        '--allowedTools',
        f'Skill({invokable_name})',
    ]
    completed = run_command(
        command,
        cwd=temporary,
        timeout=LIVE_TIMEOUT_SECONDS,
        environment=child_process_environment(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'),
        input_text=f'/{invokable_name}',
    )
    require_success(completed, context=f'Claude {installed_name} probe')
    try:
        events: tuple[Any, ...] = tuple(
            json.loads(line) for line in completed.stdout.splitlines() if line.strip()
        )
    except json.JSONDecodeError as error:
        pytest.fail(
            f'Claude {installed_name} probe returned invalid stream JSON: {error}'
        )
    advertised = any(_advertises_skill(event, invokable_name) for event in events)
    unavailable = any(
        isinstance(event, dict)
        and event.get('type') == 'result'
        and any(
            phrase in str(event.get('result', '')).lower()
            for phrase in ('not available', "don't see a skill", 'unknown skill')
        )
        for event in events
    )
    if not advertised or unavailable:
        pytest.fail(
            f'Claude did not invoke {invokable_name}; reinstall the skill '
            'and confirm the installed CLI can discover personal skills'
        )


def _probe_cli_isolation(executable: str, temporary: Path) -> None:
    """Pin isolation parsing and the effective hardened permission mode."""

    environment = child_process_environment(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1')
    permission = run_command(
        [
            executable,
            '--init-only',
            '--no-session-persistence',
            '--setting-sources',
            '',
            '--strict-mcp-config',
            '--mcp-config',
            '{"mcpServers":{}}',
            '--permission-mode',
            'dontAsk',
            '--tools',
            'Read',
            '--allowedTools',
            'Read',
        ],
        cwd=temporary,
        timeout=30,
        environment=environment,
    )
    require_success(permission, context='Claude permission-mode probe')
    assert 'Permission mode forced to default' in permission.stderr

    def invoke(mcp_configuration: str) -> subprocess.CompletedProcess[str]:
        return run_command(
            [
                executable,
                '--print',
                '--no-session-persistence',
                '--setting-sources',
                '',
                '--settings',
                _developer_settings(),
                '--strict-mcp-config',
                '--mcp-config',
                mcp_configuration,
            ],
            cwd=temporary,
            timeout=LIVE_TIMEOUT_SECONDS,
            environment=environment,
            input_text='Reply with OK.',
        )

    valid = invoke('{"mcpServers":{}}')
    valid_output = valid.stdout + valid.stderr
    assert 'Invalid MCP configuration' not in valid_output
    require_success(valid, context='Claude isolation-option probe')
    invalid = invoke('{}')
    assert 'Invalid MCP configuration' in invalid.stdout + invalid.stderr


@pytest.fixture(scope='module')
def claude_preflight(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Require an installed, authenticated CLI and an invokable role skill."""

    assert_runtime_capabilities(LIVE_CLAUDE)
    executable = shutil.which(LIVE_CLAUDE.executable)
    if executable is None:
        pytest.fail('claude executable not found on PATH')
    version = run_command([executable, '--version'], timeout=30)
    require_success(version, context='claude --version')
    auth = run_command([executable, 'auth', 'status'], timeout=30)
    require_success(auth, context='claude auth status')
    try:
        status: Any = json.loads(auth.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f'claude auth status returned invalid JSON: {error}')
    if not isinstance(status, dict) or status.get('loggedIn') is not True:
        pytest.fail('Claude authentication is unavailable; run `claude auth login`')
    _require_installed_skills()
    preflight_root = tmp_path_factory.mktemp('claude-preflight')
    _probe_cli_isolation(executable, preflight_root)
    for installed_name, invokable_name in (
        ('agent-orchestra-reviewer', CLAUDE_REVIEWER_SKILL),
        ('agent-orchestra-developer', CLAUDE_DEVELOPER_SKILL),
    ):
        skill_root = preflight_root / installed_name
        skill_root.mkdir()
        _probe_role_skill(
            executable,
            skill_root,
            installed_name=installed_name,
            invokable_name=invokable_name,
        )
    print(f'live Claude preflight: {version.stdout.strip()}')
    return executable


def test_live_claude_local_review_and_remediation(
    tmp_path: Path, claude_preflight: str
) -> None:
    """Find, remediate, and approve one deterministic temporary regression."""

    del claude_preflight
    scenario = run_local_scenario(
        tmp_path, LIVE_CLAUDE, attempt_timeout=LIVE_TIMEOUT_SECONDS
    )
    assert_local_scenario(scenario, LIVE_CLAUDE)


def test_live_claude_issue_review_uses_local_snapshot(
    tmp_path: Path,
    claude_preflight: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review deterministic issue prose without contacting or writing a provider."""

    del claude_preflight
    assert_issue_scenario(
        tmp_path,
        LIVE_CLAUDE,
        attempt_timeout=LIVE_TIMEOUT_SECONDS,
        monkeypatch=monkeypatch,
    )
