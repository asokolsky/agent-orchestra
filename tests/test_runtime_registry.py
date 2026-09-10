"""Tests for registry-owned runtime selection and role dispatch."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_orchestra import cli
from agent_orchestra.adapter.base import (
    DeveloperAdapter,
    IssueReviewerAdapter,
    IssueReviewExecution,
    ReviewerAdapter,
)
from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeDefinition,
    RuntimeRegistry,
    RuntimeRegistryError,
    RuntimeRole,
)
from agent_orchestra.agents import (
    AgentRequest,
    AgentResult,
    CommandAgentAdapter,
    DeveloperRequest,
)
from agent_orchestra.cli import build_parser
from agent_orchestra.evidence import (
    WorkerError,
    resolve_evidence_path,
)
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.manifests import parse_manifest
from agent_orchestra.models import Run, RunState
from agent_orchestra.runtime_metadata import (
    runtime_metadata_path,
)
from agent_orchestra.skill_install import install_skills
from agent_orchestra.store import JobStore
from agent_orchestra.worker import (
    WorkerContext,
    resume_review,
)


class FakeReviewer(ReviewerAdapter):
    """Executable test reviewer adapter."""

    def __init__(self, model: str | None) -> None:
        """Retain the requested model."""

        self.model = model

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Record fake source-review dispatch."""

        response_path.write_text(request_path.read_text())


class FakeDeveloper(DeveloperAdapter):
    """Executable test developer adapter."""

    def __init__(self, model: str | None) -> None:
        """Retain the requested model."""

        self.model = model

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Record fake remediation dispatch."""

        response_path.write_text(request_path.read_text())


class FakeIssueReviewer(IssueReviewerAdapter):
    """Executable test issue-review adapter."""

    def __init__(self, model: str | None) -> None:
        """Retain the requested model."""

        self.model = model

    def execute(self, request: dict[str, Any], *, timeout: int) -> IssueReviewExecution:
        """Record fake issue-review dispatch inputs."""

        return IssueReviewExecution(request, str(timeout), '', 0)


def fake_runtime(
    *,
    identifier: str = 'fake-runtime',
    developer: bool = True,
    reports_runtime_metadata: bool = False,
) -> RuntimeDefinition:
    """Return one test runtime with configurable development capability."""

    return RuntimeDefinition(
        identifier=identifier,
        vendor='example',
        module=__name__,
        reviewer_adapter=f'{__name__}.FakeReviewer',
        developer_adapter=(f'{__name__}.FakeDeveloper' if developer else None),
        issue_reviewer_adapter=f'{__name__}.FakeIssueReviewer',
        manifest_placeholders=frozenset({'schema'}),
        reports_runtime_metadata=reports_runtime_metadata,
        skill_home_environment='FAKE_RUNTIME_HOME',
        skill_home_directory='.fake-runtime',
    )


def initialize_source_review(
    tmp_path: Path,
    registry: RuntimeRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[JobStore, Run, Path, list[AgentRequest]]:
    """Start a fake-runtime review and interrupt its first remediation attempt."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(repo)], check=True, capture_output=True)
    (repo / 'tracked.txt').write_text('initial\n')
    subprocess.run([git, '-C', str(repo), 'add', 'tracked.txt'], check=True)
    subprocess.run(
        [
            git,
            '-C',
            str(repo),
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
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    digest = cli._working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    store.add(run)
    runs_directory = tmp_path / 'runs'
    requests: list[AgentRequest] = []
    developer_attempts = 0

    def execute(adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Return canonical fake responses at the command-adapter boundary."""

        del adapter
        nonlocal developer_attempts
        requests.append(request)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text())
        if isinstance(request, DeveloperRequest):
            developer_attempts += 1
            if developer_attempts == 1:
                raise subprocess.TimeoutExpired(['fake-runtime'], 1)
            (request.worktree_path / 'tracked.txt').write_text('remediated\n')
            review = json.loads(
                Path(document['payload']['review_result_path']).read_text()
            )
            response = {
                'schema_version': 1,
                'message_id': str(uuid4()),
                'in_reply_to': document['message_id'],
                'run_id': document['run_id'],
                'sequence': document['sequence'] + 1,
                'iteration': document['iteration'],
                'message_type': 'developer_handoff',
                'sender': 'developer',
                'recipient': 'orchestrator',
                'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'scope': document['scope'],
                'payload': {
                    'status': 'ready_for_review',
                    'summary': 'remediated',
                    'files_changed': ['tracked.txt'],
                    'validation': [],
                    'dispositions': [
                        {
                            'finding_id': item['finding_id'],
                            'disposition': 'addressed',
                            'rationale': 'fixed',
                        }
                        for item in review['payload']['findings']
                    ],
                    'remaining_risks': [],
                },
            }
        else:
            approved = document['iteration'] > 1
            artifact = Path(document['payload']['artifact_path'])
            artifact.write_text('# Fake review\n')
            response = {
                'schema_version': 1,
                'message_id': str(uuid4()),
                'in_reply_to': document['message_id'],
                'run_id': document['run_id'],
                'sequence': document['sequence'] + 1,
                'iteration': document['iteration'],
                'message_type': 'review_result',
                'sender': 'reviewer',
                'recipient': 'orchestrator',
                'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'scope': document['scope'],
                'payload': {
                    'verdict': 'approved' if approved else 'changes_requested',
                    'summary': 'reviewed',
                    'findings': []
                    if approved
                    else [
                        {
                            'finding_id': 'FAKE-001',
                            'severity': 'medium',
                            'title': 'fix',
                            'path': 'tracked.txt',
                            'line': 1,
                            'explanation': 'Needs correction.',
                            'acceptance_criterion': 'Correct the content.',
                        }
                    ],
                    'validation': [],
                    'verification_gaps': [],
                    'artifact_path': str(artifact),
                },
            }
        request.response_path.write_text(json.dumps(response))
        return AgentResult(
            succeeded=True,
            summary='fake runtime completed',
            stdout='',
            stderr='',
            exit_code=0,
        )

    parser = build_parser(runtime_registry=registry)
    args = parser.parse_args(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review with fake runtime.',
            '--runs-directory',
            str(runs_directory),
            '--reviewer-agent',
            'fake-runtime',
            '--developer-agent',
            'fake-runtime',
        ]
    )
    args.reviewer_command = []
    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)
    assert cli._run(args, store) == 2
    assert store.get(run.id).state is RunState.INTERRUPTED
    return store, run, runs_directory, requests


def test_registered_runtime_is_accepted_by_every_runtime_option(tmp_path: Path) -> None:
    """Expose one added runtime on every command without editing command plumbing."""

    parser = build_parser(runtime_registry=RuntimeRegistry((fake_runtime(),)))

    issue = parser.parse_args(
        ['review-issue', 'job-id', '--reviewer-agent', 'fake-runtime']
    )
    run = parser.parse_args(
        [
            'run',
            'job-id',
            '--objective',
            'Review.',
            '--reviewer-agent',
            'fake-runtime',
            '--developer-agent',
            'fake-runtime',
        ]
    )
    install = parser.parse_args(
        [
            'skills',
            'install',
            '--agent',
            'fake-runtime',
            '--skill',
            'example',
            '--source',
            str(tmp_path),
        ]
    )

    assert issue.reviewer_agent == 'fake-runtime'
    assert run.reviewer_agent == run.developer_agent == 'fake-runtime'
    assert install.agent == 'fake-runtime'


def test_omitted_runtime_options_use_registry_selected_default() -> None:
    """Keep every argparse default valid for the injected registry."""

    parser = build_parser(runtime_registry=RuntimeRegistry((fake_runtime(),)))

    issue = parser.parse_args(['review-issue', 'job-id'])
    run = parser.parse_args(['run', 'job-id', '--objective', 'Review.'])

    assert issue.reviewer_agent == 'fake-runtime'
    assert run.reviewer_agent == run.developer_agent == 'fake-runtime'


def test_role_choices_and_lookup_exclude_unsupported_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a known runtime for an undeclared role with a stable error."""

    registry = RuntimeRegistry(
        (
            fake_runtime(developer=False),
            fake_runtime(identifier='full-runtime'),
        )
    )

    assert registry.identifiers(RuntimeRole.DEVELOPER) == ('full-runtime',)
    with pytest.raises(RuntimeRegistryError, match='runtime_role_unsupported') as error:
        registry.require('fake-runtime', RuntimeRole.DEVELOPER)
    assert error.value.code == 'runtime_role_unsupported'
    parser = build_parser(runtime_registry=registry)
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                'run',
                'job-id',
                '--objective',
                'Review.',
                '--developer-agent',
                'fake-runtime',
            ]
        )
    assert 'runtime_role_unsupported' in capsys.readouterr().err


def test_fake_adapters_dispatch_every_declared_role(tmp_path: Path) -> None:
    """Dispatch source review, remediation, and issue review without branches."""

    registry = RuntimeRegistry((fake_runtime(),))
    request = tmp_path / 'request.json'
    request.write_text('{}')
    for role in (RuntimeRole.REVIEWER, RuntimeRole.DEVELOPER):
        response = tmp_path / f'{role}.json'
        adapter = cast('Any', registry.adapter('fake-runtime', role, 'model'))
        adapter.execute(request, response)
        assert response.read_text() == '{}'
        assert adapter.model == 'model'
    issue_adapter = cast(
        'Any', registry.adapter('fake-runtime', RuntimeRole.ISSUE_REVIEWER, 'model')
    )
    execution = issue_adapter.execute({'source': 'issue'}, timeout=17)
    assert execution.result == {'source': 'issue'}
    assert execution.stdout == '17'


def test_fake_manifest_and_skill_home_are_registry_driven(tmp_path: Path) -> None:
    """Validate role-limited profiles and install to the declared skill home."""

    registry = RuntimeRegistry((fake_runtime(developer=False),))
    manifest = parse_manifest(
        'fake-runtime',
        'id="fake-runtime"\nkind="runtime"\nschema_version=1\n'
        'min_engine_version=1\n[profiles]\nreviewer=["run {schema}"]\n'
        'issue_reviewer=["run {schema}"]\n',
        runtime_registry=registry,
    )
    assert set(cast('dict[str, Any]', manifest.data['profiles'])) == {
        'reviewer',
        'issue_reviewer',
    }

    source = tmp_path / 'source'
    skill = source / 'example'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('skill')
    runtime_home = tmp_path / 'fake-home'
    result = install_skills(
        ('example',),
        ('fake-runtime',),
        source_root=source,
        skill_homes={'fake-runtime': runtime_home},
        runtime_registry=registry,
    )
    assert result[0].destination == runtime_home / 'skills' / 'example'


@pytest.mark.parametrize('reports_metadata', [False, True])
def test_worker_metadata_capability_uses_selected_registry(
    tmp_path: Path, reports_metadata: bool
) -> None:
    """Dispatch runtime metadata from the same injected registry capability."""

    registry = RuntimeRegistry(
        (fake_runtime(reports_runtime_metadata=reports_metadata),)
    )
    identity = InvocationIdentity(vendor='example', model=None, runtime='fake-runtime')
    path = tmp_path / 'runtime.json'

    assert runtime_metadata_path(identity, path, registry) == (
        path if reports_metadata else None
    )


@pytest.mark.parametrize('reports_metadata', [False, True])
def test_fake_runtime_source_review_dispatches_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reports_metadata: bool,
) -> None:
    """Traverse CLI selection, worker remediation, and recoverable resume."""

    initial_registry = RuntimeRegistry(
        (fake_runtime(reports_runtime_metadata=reports_metadata),)
    )
    store, run, runs_directory, requests = initialize_source_review(
        tmp_path, initial_registry, monkeypatch
    )
    resumed_registry = RuntimeRegistry(
        (
            replace(
                fake_runtime(reports_runtime_metadata=reports_metadata),
                vendor='resumed-example',
            ),
        )
    )

    result = resume_review(
        context=WorkerContext(
            store=store,
            runs_directory=runs_directory,
            digest_worktree=cli._working_tree_digest,
            registry=resumed_registry,
        ),
        run=store.get(run.id),
    )

    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert [request.role for request in requests] == [
        'reviewer',
        'developer',
        'developer',
        'reviewer',
    ]
    assert all(
        (request.runtime_metadata_path is not None) is reports_metadata
        for request in requests
    )
    run_directory = resolve_evidence_path(runs_directory, str(run.id))
    invocation_directory = run_directory / 'invocations'
    resumed_records = [
        json.loads(path.read_text())
        for path in invocation_directory.iterdir()
        if 'attempt-0002' in path.name or path.name.startswith('000005-reviewer')
    ]
    assert {record['agent_vendor'] for record in resumed_records} == {'resumed-example'}


def test_fake_runtime_resume_rejects_removed_developer_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a persisted runtime before relaunch when its role is removed."""

    registry = RuntimeRegistry((fake_runtime(),))
    store, run, runs_directory, requests = initialize_source_review(
        tmp_path, registry, monkeypatch
    )
    calls_before_resume = len(requests)
    reviewer_only = RuntimeRegistry((fake_runtime(developer=False),))

    with pytest.raises(WorkerError, match='runtime_role_unsupported') as raised:
        resume_review(
            context=WorkerContext(
                store=store,
                runs_directory=runs_directory,
                digest_worktree=cli._working_tree_digest,
                registry=reviewer_only,
            ),
            run=store.get(run.id),
        )

    assert raised.value.code == 'runtime_role_unsupported'
    assert len(requests) == calls_before_resume
    run_directory = resolve_evidence_path(runs_directory, str(run.id))
    failure = json.loads((run_directory / 'failure.json').read_text())
    assert failure['error']['code'] == 'runtime_role_unsupported'


def test_skills_all_keeps_registry_order_and_meaning() -> None:
    """Keep all as the sentinel covering every registered runtime."""

    registry = RuntimeRegistry((fake_runtime(),))
    parsed = build_parser(runtime_registry=registry).parse_args(
        ['skills', 'install', '--agent', 'all', '--skill', 'example']
    )

    assert parsed.agent == 'all'
    assert parsed.runtime_registry.identifiers() == ('fake-runtime',)


@pytest.mark.parametrize('runtime', ['codex', 'claude-code'])
@pytest.mark.parametrize('role', list(RuntimeRole))
def test_default_registry_resolves_every_declared_adapter(
    runtime: str, role: RuntimeRole
) -> None:
    """Resolve each built-in role through its registered implementation."""

    adapter = DEFAULT_RUNTIME_REGISTRY.adapter(runtime, role, 'test-model')

    assert cast('Any', adapter).model == 'test-model'
