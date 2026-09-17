"""Opt-in end-to-end checks for the installed Claude Code runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import TYPE_CHECKING, Any, cast

import pytest

from agent_orchestra.adapter.claude_code import (
    CLAUDE_DEVELOPER_SKILL,
    CLAUDE_REVIEWER_SKILL,
    _developer_settings,
    _stage_skill,
)
from agent_orchestra.evidence import resolve_evidence_path
from agent_orchestra.issue_review import run_issue_review
from agent_orchestra.issue_sources import IssueLocator, IssueSnapshot, write_snapshot
from agent_orchestra.models import IssueJob, RunState
from agent_orchestra.runtime_metadata import child_process_environment
from agent_orchestra.skill_install import skill_destination
from agent_orchestra.store import JobStore
from tests.live.runtime_harness import (
    LiveRuntime,
    git,
    invocation_records,
    require_success,
    run_command,
    run_local_scenario,
    verified_audit,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

LIVE_CLAUDE = LiveRuntime(
    identifier='claude-code',
    executable='claude',
    opt_in_environment='AGENT_ORCHESTRA_LIVE_CLAUDE',
    model_environment='AGENT_ORCHESTRA_LIVE_CLAUDE_MODEL',
    skill_names=('agent-orchestra-reviewer', 'agent-orchestra-developer'),
)
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
    store = JobStore(scenario.database)
    job = store.get(scenario.job_id)

    assert job.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert job.iteration >= 2
    assert git(scenario.repository, 'rev-list', '--count', 'HEAD') == '1'
    assert git(scenario.repository, 'status', '--short') == 'M calculator.py'
    assert 'return left + right' in (scenario.repository / 'calculator.py').read_text(
        encoding='utf-8'
    )
    tests = run_command(
        [shutil.which('python') or 'python', '-m', 'unittest'],
        cwd=scenario.repository,
        timeout=30,
    )
    require_success(tests, context='remediated fixture validation')

    records = invocation_records(scenario.runs_directory, scenario.job_id)
    assert [record['role'] for record in records] == [
        'reviewer',
        'developer',
        'reviewer',
    ]
    assert all(record['runtime'] == LIVE_CLAUDE.identifier for record in records)
    assert all(record['conclusion'] == 'succeeded' for record in records)
    assert all(record['effective_models'] for record in records)
    assert all(record['effective_model_status'] == 'reported' for record in records)
    verified_audit(store, scenario.job_id, scenario.runs_directory)


def test_live_claude_issue_review_uses_local_snapshot(
    tmp_path: Path,
    claude_preflight: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review deterministic issue prose without reading or writing a provider."""

    del claude_preflight
    body = (
        'Problem: reject empty widget names before persistence.\n\n'
        'Scope: update only the create-widget validation path and its unit tests.\n\n'
        'Constraints: preserve the public API and existing error codes.\n\n'
        'Acceptance criteria:\n'
        '- whitespace-only names return invalid_widget_name;\n'
        '- valid names continue to be persisted;\n'
        '- unit tests cover both behaviors.\n'
    )
    digest = f'sha256:{hashlib.sha256(body.encode()).hexdigest()}'
    snapshot = IssueSnapshot(
        locator=IssueLocator(
            provider='github',
            host='github.com',
            namespace='agent-orchestra-live',
            project='fixture',
            number=1,
            url='https://github.com/agent-orchestra-live/fixture/issues/1',
        ),
        title='Reject empty widget names',
        body=body,
        author='live-test',
        labels=('test',),
        state='open',
        created_at='2026-01-01T00:00:00Z',
        updated_at='2026-01-01T00:00:00Z',
        digest=digest,
    )
    job = IssueJob.create(
        provider=snapshot.locator.provider,
        host=snapshot.locator.host,
        remote_url=snapshot.locator.url,
        namespace=snapshot.locator.namespace,
        project=snapshot.locator.project,
        issue_number=snapshot.locator.number,
        title=snapshot.title,
        author=snapshot.author,
        source_updated_at=snapshot.updated_at,
        source_digest=snapshot.digest,
    )
    database = tmp_path / 'state' / 'state.db'
    runs_directory = tmp_path / 'evidence'
    store = JobStore(database)
    store.initialize()
    store.add_issue(job)
    write_snapshot(
        runs_directory,
        job.id,
        resolve_evidence_path(runs_directory, job.id) / 'issue.json',
        snapshot,
    )
    fetches: list[str] = []

    def fetch_local(url: str) -> IssueSnapshot:
        """Return the immutable fixture and record every attempted provider read."""

        fetches.append(url)
        return snapshot

    monkeypatch.setattr('agent_orchestra.issue_review.fetch_issue', fetch_local)
    finished = run_issue_review(
        job,
        store,
        runs_directory,
        objective='Review this issue for implementation readiness.',
        agent=LIVE_CLAUDE.identifier,
        model=LIVE_CLAUDE.requested_model,
        timeout=LIVE_TIMEOUT_SECONDS,
    )

    assert finished.state in {RunState.APPROVED, RunState.CHANGES_REQUESTED}
    assert fetches == [snapshot.locator.url, snapshot.locator.url]
    result_path = (
        resolve_evidence_path(runs_directory, job.id) / 'iterations/000001/result.json'
    )
    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert result['source_digest'] == snapshot.digest
    records = invocation_records(runs_directory, job.id)
    assert len(records) == 1
    assert records[0]['role'] == 'issue_reviewer'
    assert records[0]['runtime'] == LIVE_CLAUDE.identifier
    assert records[0]['conclusion'] == 'succeeded'
    assert records[0]['effective_models']
    assert records[0]['effective_model_status'] == 'reported'
    assert store.list_issue_actions(job.id) == ()
    audit = verified_audit(store, job.id, runs_directory)
    evidence = cast('list[dict[str, object]]', audit['evidence'])
    assert any(
        item['evidence_type'] == 'issue_feedback' and item['status'] == 'verified'
        for item in evidence
    )
