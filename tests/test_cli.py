"""Tests for common command-line behavior and administration."""

from __future__ import annotations

import json
from importlib.metadata import version
from typing import TYPE_CHECKING

import pytest

from agent_orchestra import (
    cli,
)
from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.adapter.registry import (
    RuntimeRegistryError,
)
from agent_orchestra.cli import (
    main,
)
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.invocations import (
    InvocationEvidenceError,
)
from agent_orchestra.issue_review import IssueReviewError
from agent_orchestra.manifests import ENGINE_TOO_OLD, ManifestError
from agent_orchestra.reviewer_plan import ReviewerPlanError
from agent_orchestra.schemas import SchemaValidationError
from agent_orchestra.store import JobStore, RunNotFoundError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (RunNotFoundError('missing'), ('job_not_found', 'job not found: missing')),
        (
            IssueReviewerError('timed out', timed_out=True),
            ('issue_review_timed_out', 'timed out'),
        ),
        (
            IssueReviewerError('interrupted', interrupted=True),
            ('issue_review_interrupted', 'interrupted'),
        ),
        (
            IssueReviewerError('failed'),
            ('issue_reviewer_failed', 'failed'),
        ),
        (
            SchemaValidationError('invalid result'),
            ('issue_review_result_invalid', 'invalid result'),
        ),
        (
            InvocationEvidenceError('invalid evidence'),
            ('invalid_evidence', 'invalid evidence'),
        ),
        (
            IssueReviewError('review failed'),
            ('issue_review_failed', 'review failed'),
        ),
    ],
)
def test_issue_review_error_codes(
    error: BaseException, expected: tuple[str, str]
) -> None:
    """Pin every public issue-review classifier result."""

    assert cli._issue_review_error(error) == expected


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (RunNotFoundError('missing'), ('job_not_found', 'job not found: missing')),
        (
            RuntimeRegistryError('runtime_unknown', 'missing'),
            ('runtime_unknown', 'missing'),
        ),
        (
            WorkerError('specific failure', code='worker_specific'),
            ('worker_specific', 'specific failure'),
        ),
        (WorkerError('worker failure'), ('worker_error', 'worker failure')),
        (
            ReviewerPlanError('invalid plan'),
            ('reviewer_plan_invalid', 'invalid plan'),
        ),
        (OSError('run failed'), ('run_failed', 'run failed')),
    ],
)
def test_run_error_codes(error: BaseException, expected: tuple[str, str]) -> None:
    """Pin every public run classifier result."""

    assert cli._run_error(error) == expected


def test_cli_rejects_incompatible_manifest_with_stable_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail before argument handling when packaged knowledge is incompatible."""

    def reject_manifests() -> None:
        raise ManifestError(ENGINE_TOO_OLD, 'codex')

    monkeypatch.setattr(cli, 'validate_packaged_manifests', reject_manifests)
    assert main(['--help']) == 2
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': cli.CLI_SCHEMA_VERSION,
        'agent_orchestra_version': version('py-agent-orchestra'),
        'error': {
            'code': 'manifest_engine_too_old',
            'message': 'manifest_engine_too_old: codex',
        },
    }


def test_version_reports_installed_distribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report the package version without requiring a subcommand."""

    with pytest.raises(SystemExit) as raised:
        main(['--version'])

    assert raised.value.code == 0
    assert capsys.readouterr().out == (
        f'agent-orchestra {version("py-agent-orchestra")}\n'
    )


def test_skills_install_for_both_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install a requested skill through the public CLI."""

    source = tmp_path / 'source'
    skill = source / 'example-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('instructions\n')
    codex_home = tmp_path / 'codex=custom'
    claude_home = tmp_path / 'claude'

    result = main(
        [
            'skills',
            'install',
            '--agent',
            'all',
            '--skill',
            'example-skill',
            '--source',
            str(source),
            '--skill-home',
            f'codex={codex_home}',
            '--skill-home',
            f'claude-code={claude_home}',
        ]
    )

    assert result == 0
    assert 'installed example-skill for codex' in capsys.readouterr().out
    assert (codex_home / 'skills/example-skill/SKILL.md').is_file()
    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()


@pytest.mark.parametrize(
    ('override', 'message'),
    [
        ('codex', 'expected RUNTIME=PATH'),
        ('codex=', 'expected RUNTIME=PATH'),
        ('=/tmp/skills', 'runtime_unknown: '),
        ('unknown=/tmp/skills', 'runtime_unknown: unknown'),
    ],
)
def test_skills_install_rejects_malformed_home_override(
    override: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject malformed or unknown runtime skill-home overrides."""

    with pytest.raises(SystemExit) as error:
        main(
            [
                'skills',
                'install',
                '--skill',
                'agent-orchestra-developer',
                '--skill-home',
                override,
            ]
        )

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_skills_install_rejects_duplicate_home_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject two skill-home overrides for the same runtime."""

    result = main(
        [
            'skills',
            'install',
            '--skill',
            'agent-orchestra-developer',
            '--skill-home',
            f'codex={tmp_path / "first"}',
            '--skill-home',
            f'codex={tmp_path / "second"}',
        ]
    )

    assert result == 2
    assert 'skill home specified twice for codex' in capsys.readouterr().err


@pytest.mark.parametrize(
    'command',
    [
        ['jobs'],
        ['prune'],
        ['config', 'show'],
    ],
)
def test_command_documents_report_the_cli_schema_version_and_the_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    """Report CLI_SCHEMA_VERSION and the producing build from each command."""

    # The prune document carried a literal of its own and sat nine versions
    # behind the rest of the CLI without any test noticing. This asserts the
    # property that was missing rather than the one document that broke it, so
    # a new command cannot reintroduce the drift. The audit document is out of
    # scope here: it carries the independent AUDIT_SCHEMA_VERSION, and
    # tests/test_audit.py owns its contract.
    database = tmp_path / 'state.db'
    JobStore(database).initialize()
    runs = ['--runs-directory', str(tmp_path / 'runs')] if command != ['jobs'] else []

    assert main(['--database', str(database), *command, *runs]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == cli.CLI_SCHEMA_VERSION
    assert document['agent_orchestra_version'] == version('py-agent-orchestra')
