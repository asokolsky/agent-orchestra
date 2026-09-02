"""Tests for command-line operations."""

import json
import re
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra.cli import DEFAULT_DATABASE, _working_tree_digest, main
from agent_orchestra.models import Run
from agent_orchestra.store import RunStore


def initialize_git_repository(path: Path) -> None:
    """Create a repository with one committed file."""

    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(path)], check=True, capture_output=True)
    (path / 'tracked.txt').write_text('initial\n')
    subprocess.run([git, '-C', str(path), 'add', 'tracked.txt'], check=True)
    subprocess.run(
        [
            git,
            '-C',
            str(path),
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-m',
            'initial',
        ],
        check=True,
        capture_output=True,
    )


def write_reviewer(path: Path, verdict: str) -> None:
    """Write a deterministic reviewer command for CLI integration tests."""

    path.write_text(
        f'''"""Test reviewer command."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

request_path = Path(sys.argv[1])
response_path = Path(sys.argv[2])
request = json.loads(request_path.read_text())
artifact_path = Path(request["payload"]["artifact_path"])
artifact_path.write_text("# Review\\n")
response = {{
    "schema_version": 1,
    "message_id": str(uuid4()),
    "in_reply_to": request["message_id"],
    "run_id": request["run_id"],
    "sequence": 2,
    "iteration": request["iteration"],
    "message_type": "review_result",
    "sender": "reviewer",
    "recipient": "orchestrator",
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "scope": request["scope"],
    "payload": {{
        "verdict": "{verdict}",
        "summary": "reviewed",
        "findings": [] if "{verdict}" == "approved" else [{{
            "finding_id": "F-001",
            "severity": "medium",
            "title": "fix",
            "path": "tracked.txt",
            "line": 1,
            "explanation": "Needs correction.",
            "acceptance_criterion": "Correct the content.",
        }}],
        "validation": [],
        "verification_gaps": [],
        "artifact_path": str(artifact_path),
    }},
}}
response_path.write_text(json.dumps(response))
'''
    )


def test_init_creates_database(tmp_path: Path) -> None:
    """Initialize the requested state database."""

    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'init'])

    assert result == 0
    assert database.exists()


def test_enqueue_local_captures_current_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a digest for tracked and untracked local changes."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    (repository / 'untracked.txt').write_text('new\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repository)])

    assert result == 0
    run = RunStore(database).list_runs()[0]
    assert re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', run.id)
    assert run.diff_digest is not None
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', run.diff_digest)
    assert run.base_sha == run.head_sha
    assert str(run.id) in capsys.readouterr().out


def test_untracked_executable_mode_changes_working_tree_digest(tmp_path: Path) -> None:
    """Bind approval digests to executable-mode changes on untracked files."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    script = repository / 'script.sh'
    script.write_text('#!/bin/sh\n')
    before = _working_tree_digest(repository, 'HEAD')

    script.chmod(script.stat().st_mode | 0o100)
    after = _working_tree_digest(repository, 'HEAD')

    assert before is not None
    assert after is not None
    assert after != before


def test_enqueue_local_reports_git_failure_without_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable error for a path that is not a Git repository."""

    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(tmp_path)])

    assert result == 2
    assert 'error:' in capsys.readouterr().err
    assert not database.exists()


def test_enqueue_local_rejects_clean_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not enqueue an empty local-change scope."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repository)])

    assert result == 2
    assert 'no local changes' in capsys.readouterr().err
    assert not database.exists()


def test_enqueue_locals_captures_changed_child_repositories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enqueue changed immediate child repos while skipping clean repos."""

    projects = tmp_path / 'projects'
    projects.mkdir()
    changed_b = projects / 'changed-b'
    changed_a = projects / 'changed-a'
    clean = projects / 'clean'
    not_a_repository = projects / 'notes'
    for repository in (changed_b, changed_a, clean):
        repository.mkdir()
        initialize_git_repository(repository)
    not_a_repository.mkdir()
    (changed_a / 'tracked.txt').write_text('changed a\n')
    (changed_b / 'untracked.txt').write_text('changed b\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    runs = RunStore(database).list_runs()
    assert {run.worktree_path for run in runs} == {changed_a, changed_b}
    output = capsys.readouterr().out
    assert output.index(str(changed_a)) < output.index(str(changed_b))
    assert (
        'enqueued 2 repositories; skipped 1 clean repository; failed 0 repositories'
    ) in output


def test_enqueue_locals_accepts_tilde_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expand a leading tilde in the projects directory argument."""

    projects = tmp_path / 'Projects'
    repository = projects / 'repo'
    repository.mkdir(parents=True)
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', '~/Projects'])

    assert result == 0
    assert RunStore(database).list_runs()[0].worktree_path == repository
    assert 'enqueued 1 repository' in capsys.readouterr().out


def test_enqueue_locals_with_no_changed_repositories_does_not_create_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Succeed without state when no child repository has local changes."""

    projects = tmp_path / 'projects'
    repository = projects / 'clean'
    repository.mkdir(parents=True)
    initialize_git_repository(repository)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert not database.exists()
    assert (
        'enqueued 0 repositories; skipped 1 clean repository; failed 0 repositories'
    ) in capsys.readouterr().out


def test_enqueue_locals_continues_after_repository_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enqueue valid changes despite an unreadable sibling repository."""

    projects = tmp_path / 'projects'
    changed = projects / 'changed'
    fresh = projects / 'fresh'
    changed.mkdir(parents=True)
    fresh.mkdir()
    initialize_git_repository(changed)
    (changed / 'tracked.txt').write_text('changed\n')
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(fresh)], check=True, capture_output=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert RunStore(database).list_runs()[0].worktree_path == changed
    captured = capsys.readouterr()
    assert f'error: {fresh}:' in captured.err
    assert (
        'enqueued 1 repository; skipped 0 clean repositories; failed 1 repository'
    ) in captured.out


def test_enqueue_locals_fails_when_every_repository_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return nonzero when no repository can be enqueued and one fails."""

    projects = tmp_path / 'projects'
    fresh = projects / 'fresh'
    fresh.mkdir(parents=True)
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(fresh)], check=True, capture_output=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 2
    assert not database.exists()
    captured = capsys.readouterr()
    assert f'error: {fresh}:' in captured.err
    assert 'failed 1 repository' in captured.out


def test_enqueue_locals_reports_directory_without_repositories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish an empty repository scan from an all-clean scan."""

    projects = tmp_path / 'projects'
    (projects / 'notes').mkdir(parents=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert not database.exists()
    assert f'no Git repositories found in {projects}' in capsys.readouterr().out


def test_enqueue_locals_rejects_missing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a stable error when the projects directory does not exist."""

    database = tmp_path / 'state.db'

    result = main(
        ['--database', str(database), 'enqueue-locals', str(tmp_path / 'missing')]
    )

    assert result == 2
    assert 'directory not found' in capsys.readouterr().err
    assert not database.exists()


def test_status_does_not_create_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep status read-only when no state database exists."""

    database = tmp_path / 'missing' / 'state.db'

    result = main(['--database', str(database), 'status'])

    assert result == 2
    assert 'state database not found' in capsys.readouterr().err
    assert not database.exists()


def test_status_lists_persisted_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Display a persisted run without changing state."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)

    result = main(['--database', str(database), 'status'])

    assert result == 0
    output = capsys.readouterr().out
    assert output.startswith('{\n  "schema_version": 1,\n  "runs": [\n    {\n')
    assert output.endswith('\n}\n')
    document = json.loads(output)
    assert set(document['runs'][0]) == {field.name for field in fields(Run)}
    assert document == {
        'schema_version': 1,
        'runs': [
            {
                'id': str(run.id),
                'scenario': 'local_changes',
                'repository_path': str(tmp_path),
                'worktree_path': str(tmp_path),
                'state': 'queued',
                'base_sha': 'base',
                'head_sha': 'head',
                'diff_digest': 'digest',
                'iteration': 0,
                'remote_url': None,
                'created_at': run.created_at.isoformat().replace('+00:00', 'Z'),
                'updated_at': run.updated_at.isoformat().replace('+00:00', 'Z'),
            }
        ],
    }


def test_status_filters_json_document_by_run_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep single-run status output in the versioned runs envelope."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    first = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'first')
    second = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'second')
    store.add(first)
    store.add(second)

    result = main(['--database', str(database), 'status', str(first.id)])

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 1
    assert [run['id'] for run in document['runs']] == [str(first.id)]


def test_status_lists_empty_runs_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable empty collection for an initialized database."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()

    result = main(['--database', str(database), 'status'])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 1,
        'runs': [],
    }


def test_status_reports_unknown_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a distinct error when the requested run does not exist."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()

    result = main(['--database', str(database), 'status', str(uuid4())])

    assert result == 2
    assert 'run not found' in capsys.readouterr().err


def test_run_dispatches_review_and_awaits_commit_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a correlated approved review and stop at the commit gate."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    runs_directory = tmp_path / 'runs'

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(runs_directory),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'awaiting_commit_authorization'
    run_directory = runs_directory / str(run.id)
    assert (run_directory / 'messages/000001-review-request.json').is_file()
    assert (run_directory / 'messages/000002-review-result.json').is_file()
    assert (run_directory / 'artifacts/review-0001.md').is_file()
    assert (
        json.loads(capsys.readouterr().out)['state'] == 'awaiting_commit_authorization'
    )


def test_run_uses_builtin_codex_adapter_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select the packaged Codex adapter when no custom command is supplied."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting Codex."""

        observed.update(kwargs)
        return run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
        ]
    )

    assert result == 0
    assert observed['reviewer_command'] == [
        sys.executable,
        '-m',
        'agent_orchestra.codex_reviewer',
    ]


def test_default_database_is_outside_a_repo_in_the_home_directory() -> None:
    """Keep default orchestration state outside a typical reviewed repo."""

    repository = Path.home() / 'Projects/repo'

    assert Path.home() / '.local/state/agent-orchestra/state.db' == DEFAULT_DATABASE
    assert not DEFAULT_DATABASE.is_relative_to(repository)


def test_run_passes_explicit_codex_model_to_builtin_adapter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow callers to avoid an incompatible managed Codex model default."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting Codex."""

        observed.update(kwargs)
        return run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--codex-model',
            'compatible-model',
        ]
    )

    assert result == 0
    assert observed['reviewer_command'] == [
        sys.executable,
        '-m',
        'agent_orchestra.codex_reviewer',
        '--model',
        'compatible-model',
    ]


def test_run_records_requested_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advance a rejected review to the remediation boundary."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'changes_requested'


def test_run_keeps_blocked_review_awaiting_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a blocked review without inventing a terminal state."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'blocked')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'awaiting_review'


@pytest.mark.parametrize(
    'reviewer_command', [['/missing/reviewer'], ['/usr/bin/false']]
)
def test_run_marks_reviewer_execution_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reviewer_command: list[str],
) -> None:
    """Persist failed state for missing and nonzero reviewer commands."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            *reviewer_command,
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'


def test_run_marks_reviewer_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist failed state when the bounded reviewer exceeds its timeout."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'slow.py'
    reviewer.write_text('"""Slow test reviewer."""\nimport time\ntime.sleep(5)\n')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--timeout',
            '1',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'


def test_run_rejects_state_database_inside_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep mutable orchestration state outside the reviewed worktree."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = repository / '.agent-orchestra/state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'state database must be outside' in capsys.readouterr().err


def test_run_rejects_evidence_directory_inside_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep mutable review messages and artifacts outside the reviewed worktree."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(repository / 'runs'),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'evidence directory must be outside' in capsys.readouterr().err


def test_run_handles_digest_failure_before_transition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a disappeared repo without a traceback or state mutation."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    shutil.rmtree(repository / '.git')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--',
            '/usr/bin/true',
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'cannot compute worktree digest' in capsys.readouterr().err


def test_run_marks_post_review_digest_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail durably when Git state disappears during reviewer execution."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repository)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    with reviewer.open('a') as file:
        file.write('\nimport shutil\nshutil.rmtree(Path.cwd() / ".git")\n')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'


def test_skills_install_for_both_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install a requested skill through the public CLI."""

    source = tmp_path / 'source'
    skill = source / 'example-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('instructions\n')
    codex_home = tmp_path / 'codex'
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
            '--codex-home',
            str(codex_home),
            '--claude-home',
            str(claude_home),
        ]
    )

    assert result == 0
    assert 'installed example-skill for codex' in capsys.readouterr().out
    assert (codex_home / 'skills/example-skill/SKILL.md').is_file()
    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()
