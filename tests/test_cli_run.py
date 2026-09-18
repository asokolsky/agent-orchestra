"""Tests for command-line review and remediation execution."""

import json
import os
import shutil
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra import (
    cli,
)
from agent_orchestra.adapter.registry import (
    RuntimeDefinition,
    RuntimeRegistry,
)
from agent_orchestra.agents import AgentRequest, AgentResult, CommandAgentAdapter
from agent_orchestra.audit import AUDIT_SCHEMA_VERSION, _canonical_evidence_type
from agent_orchestra.cli import (
    DEFAULT_DATABASE,
    _working_tree_digest,
    main,
)
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.execution_context import (
    ITERATION_LIMIT,
    ReviewerSetReviewPlan,
    ReviewPlan,
    WorkerContext,
)
from agent_orchestra.invocations import (
    InvocationIdentity,
)
from agent_orchestra.messages import (
    NO_REMEDIATION_CHANGE,
)
from agent_orchestra.models import Run, RunState
from agent_orchestra.queued_review import (
    run_queued_review,
)
from agent_orchestra.reviewer_plan import ReviewerExecutionPlan
from agent_orchestra.store import JobStore
from tests.cli_helpers import (
    CliRunContext,
    configure_agent,
    create_worker_run,
    evidence_directory,
    initialize_git_repo,
    resume_arguments,
    run_arguments,
    write_developer,
    write_fake_codex,
    write_loop_reviewer,
    write_provenance_reviewer,
    write_recoverable_developer,
    write_reviewer,
)


@pytest.mark.parametrize(
    ('identity', 'registry', 'expected_code'),
    [
        (
            InvocationIdentity(vendor='example', model=None, runtime='unknown'),
            RuntimeRegistry(
                (
                    RuntimeDefinition(
                        identifier='known',
                        vendor='example',
                        module='example',
                        reviewer_adapter='example.Reviewer',
                        developer_adapter=None,
                        issue_reviewer_adapter=None,
                        manifest_placeholders=frozenset(),
                        reports_runtime_metadata=False,
                        skill_home_environment='EXAMPLE_HOME',
                        skill_home_directory='.example',
                    ),
                )
            ),
            'runtime_unknown',
        ),
        (
            InvocationIdentity(vendor='example', model=None, runtime='known'),
            RuntimeRegistry(
                (
                    RuntimeDefinition(
                        identifier='known',
                        vendor='example',
                        module='example',
                        reviewer_adapter=None,
                        developer_adapter=None,
                        issue_reviewer_adapter=None,
                        manifest_placeholders=frozenset(),
                        reports_runtime_metadata=False,
                        skill_home_environment='EXAMPLE_HOME',
                        skill_home_directory='.example',
                    ),
                )
            ),
            'runtime_role_unsupported',
        ),
    ],
)
def test_fresh_worker_rejects_invalid_runtime_before_command(
    tmp_path: Path,
    identity: InvocationIdentity,
    registry: RuntimeRegistry,
    expected_code: str,
) -> None:
    """Validate programmatic runtime identities before changing durable state."""

    context = create_worker_run(tmp_path)
    marker = tmp_path / 'executed'

    with pytest.raises(WorkerError) as raised:
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
                registry=registry,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review the change.',
                reviewer_command=(sys.executable, '-c', f'open({str(marker)!r}, "w")'),
                developer_command=(),
                timeout_seconds=30,
                reviewer_identity=identity,
            ),
        )

    assert raised.value.code == expected_code
    assert context.store.get(str(context.run.id)).state is RunState.QUEUED
    assert not marker.exists()


def test_run_dispatches_review_and_awaits_commit_authorization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Persist a correlated approved review and stop at the commit gate."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    run_directory = evidence_directory(enqueued_run)
    assert (run_directory / 'messages/000001-review-request.json').is_file()
    assert (run_directory / 'messages/000002-review-result.json').is_file()
    assert (run_directory / 'artifacts/review-0001.md').is_file()
    execution = json.loads((run_directory / 'execution.json').read_text())
    assert execution == {
        'schema_version': 2,
        'run_id': str(enqueued_run.run.id),
        'objective': 'Review the change.',
        'reviewer': {
            'command': [sys.executable, str(reviewer)],
            'identity': {
                'vendor': 'unknown',
                'model': None,
                'runtime': 'custom-command',
            },
            'timeout_seconds': 1800,
        },
        'developer': {
            'command': [],
            'identity': {
                'vendor': 'openai',
                'model': None,
                'runtime': 'codex',
            },
            'timeout_seconds': 1800,
        },
        'max_review_iterations': 3,
        'created_at': execution['created_at'],
    }
    invocation = json.loads(
        (run_directory / 'invocations/000001-reviewer.json').read_text()
    )
    assert invocation['run_id'] == str(enqueued_run.run.id)
    assert invocation['role'] == 'reviewer'
    assert invocation['agent_vendor'] == 'unknown'
    assert invocation['runtime'] == 'custom-command'
    assert invocation['exit_code'] == 0
    assert invocation['timed_out'] is False
    integrity = json.loads((run_directory / '.integrity.json').read_text())
    indexed_paths = {entry['path'] for entry in integrity['entries']}
    indexed_types = {
        entry['path']: entry['evidence_type'] for entry in integrity['entries']
    }
    assert indexed_types['messages/000002-review-result.json'] == 'review_result'
    assert {
        'execution.json',
        'messages/000001-review-request.json',
        'messages/000002-review-result.json',
        'artifacts/review-0001.md',
        'invocations/000001-reviewer.json',
        'logs/000001-reviewer.stdout.log',
        'logs/000001-reviewer.stderr.log',
    } <= indexed_paths
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 23,
        'agent_orchestra_version': version('agent-orchestra'),
        'job_id': str(enqueued_run.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }
    assert (
        main(
            [
                '--database',
                str(enqueued_run.database),
                'audit',
                str(enqueued_run.run.id),
                '--runs-directory',
                str(enqueued_run.runs_directory),
                '--verify',
            ]
        )
        == 0
    )
    audit = json.loads(capsys.readouterr().out)
    assert audit['result'] == 'verified', audit['findings']
    # The audit document is published on the same channel but carries its own
    # version sequence, so it reports AUDIT_SCHEMA_VERSION beside the build
    # rather than CLI_SCHEMA_VERSION.
    assert audit['schema_version'] == AUDIT_SCHEMA_VERSION
    assert audit['schema_version'] != cli.CLI_SCHEMA_VERSION
    assert audit['agent_orchestra_version'] == version('agent-orchestra')


def test_run_persists_reported_effective_model_metadata(tmp_path: Path) -> None:
    """Consume normal-exit provenance before opening the commit gate."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_provenance_reviewer(reviewer)

    result = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review the change.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(),
            timeout_seconds=30,
            reviewer_identity=InvocationIdentity(
                vendor='anthropic', model='requested-model', runtime='claude-code'
            ),
        ),
    )

    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    run_directory = evidence_directory(context)
    invocation = json.loads(
        (run_directory / 'invocations/000001-reviewer.json').read_text()
    )
    assert invocation['schema_version'] == 4
    assert invocation['task_id'] == f'{context.run.id}:000001-reviewer'
    assert invocation['invocation_id'] == (
        f'{context.run.id}:000001-reviewer:attempt-0001'
    )
    assert invocation['status'] == 'completed'
    assert invocation['conclusion'] == 'succeeded'
    assert invocation['response_received_at'] is not None
    assert invocation['validation_started_at'] is not None
    assert invocation['requested_model'] == 'requested-model'
    assert invocation['effective_models'] == ['claude-primary', 'claude-fallback']
    assert invocation['effective_model_status'] == 'reported'
    assert not tuple(run_directory.glob('.*.runtime.json'))


def test_run_preserves_non_utf8_reviewer_output(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Archive the exact bytes written by a redirected reviewer process."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    reviewer.write_bytes(
        reviewer.read_bytes().replace(
            b'import sys\n',
            b'import sys\nsys.stdout.buffer.write(b"caf\\xe9 latin-1 byte\\n")\n'
            b'sys.stdout.buffer.flush()\n',
            1,
        )
    )
    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    stdout_log = evidence_directory(enqueued_run) / 'logs/000001-reviewer.stdout.log'
    assert stdout_log.read_bytes() == b'caf\xe9 latin-1 byte\n'


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_persists_interrupted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupted_role: str,
) -> None:
    """Keep pre-activation interruption uncertain and non-retryable."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_loop_reviewer(reviewer)
    original_execute = CommandAgentAdapter.execute

    def interrupt_selected(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Interrupt only the requested role."""

        if request.role == interrupted_role:
            raise KeyboardInterrupt
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', interrupt_selected)
    with pytest.raises(KeyboardInterrupt):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=('unused-developer',),
                timeout_seconds=30,
            ),
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_files = sorted(
        (evidence_directory(context) / 'invocations').glob('*.json')
    )
    invocation_names = [path.name for path in invocation_files]
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['status'] == 'pending'
    assert invocation['conclusion'] is None
    assert invocation['interrupted'] is False

    assert main(resume_arguments(context)) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['error']['code'] == 'resume_activation_uncertain'
    assert (
        sorted(
            path.name
            for path in (evidence_directory(context) / 'invocations').glob('*.json')
        )
        == invocation_names
    )


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_finalizes_interruption_after_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_role: str,
) -> None:
    """Complete interrupted evidence once process activation is durable."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_loop_reviewer(reviewer)
    original_execute = CommandAgentAdapter.execute

    def interrupt_selected(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Persist activation before interrupting the selected role."""

        if request.role == interrupted_role:
            assert request.on_started is not None
            request.on_started()
            raise KeyboardInterrupt
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', interrupt_selected)
    with pytest.raises(KeyboardInterrupt):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=('unused-developer',),
                timeout_seconds=30,
            ),
        )

    invocation_files = sorted(
        (evidence_directory(context) / 'invocations').glob('*.json')
    )
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['status'] == 'completed'
    assert invocation['conclusion'] == 'interrupted'
    assert invocation['interrupted'] is True


@pytest.mark.parametrize(
    'case',
    [
        ((), 'agent_orchestra.adapter.codex', 'openai', None, 'codex'),
        (
            ('--reviewer-model', 'compatible-model'),
            'agent_orchestra.adapter.codex',
            'openai',
            'compatible-model',
            'codex',
        ),
        (
            ('--reviewer-agent', 'claude-code', '--reviewer-model', 'sonnet'),
            'agent_orchestra.adapter.claude_code',
            'anthropic',
            'sonnet',
            'claude-code',
        ),
    ],
)
def test_run_selects_reviewer_adapter(
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[tuple[str, ...], str, str, str | None, str],
) -> None:
    """Build the requested reviewer command and identity independently."""

    options, module, vendor, model, runtime = case
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting an agent."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    assert main(run_arguments(enqueued_run, *options)) == 0

    expected_command = [sys.executable, '-m', module]
    if model is not None:
        expected_command.extend(['--model', model])
    plan = observed['plan']
    assert isinstance(plan, ReviewPlan)
    assert plan.reviewer_command == expected_command
    assert plan.reviewer_identity == InvocationIdentity(
        vendor=vendor, model=model, runtime=runtime
    )


@pytest.mark.parametrize(
    'case',
    [
        (
            (),
            'claude-code',
            'agent_orchestra.adapter.claude_code',
            'anthropic',
        ),
        (
            ('--developer-agent', 'codex', '--reviewer-agent', 'codex'),
            'codex',
            'agent_orchestra.adapter.codex',
            'openai',
        ),
    ],
)
def test_run_uses_configured_runtime_defaults_and_cli_overrides(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[tuple[str, ...], str, str, str],
) -> None:
    """Dispatch configured runtimes unless explicit run options override them."""

    options, runtime, module, vendor = case
    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[defaults]\n'
        'developer_runtime = "claude-code"\n'
        'reviewer_runtime = "claude-code"\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected commands without starting either agent."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    assert main(run_arguments(enqueued_run, *options)) == 0

    plan = observed['plan']
    assert isinstance(plan, ReviewPlan)
    assert plan.developer_command == [
        sys.executable,
        '-m',
        module,
        '--role',
        'developer',
    ]
    assert plan.reviewer_command == [sys.executable, '-m', module]
    identity = InvocationIdentity(vendor=vendor, model=None, runtime=runtime)
    assert plan.developer_identity == identity
    assert plan.reviewer_identity == identity


def test_run_selects_configured_reviewer_set(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve a named reviewer set into the worker's immutable batch plan."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[defaults]\nreviewer_runtime = "claude-code"\n\n'
        '[reviewer_sets.default]\n'
        'members = [\n'
        '  { id = "security", runtime = "codex" },\n'
        '  { id = "portability", runtime = "claude-code" },\n'
        ']\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    observed: dict[str, object] = {}

    def review_set(**kwargs: object) -> Run:
        """Capture the selected batch without starting its reviewers."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_reviewer_set', review_set)

    assert main(run_arguments(enqueued_run, '--reviewer-set', 'default')) == 0

    plan = observed['plan']
    assert isinstance(plan, ReviewerSetReviewPlan)
    assert isinstance(plan.reviewer_plan, ReviewerExecutionPlan)
    assert [reviewer.reviewer_id for reviewer in plan.reviewer_plan.reviewers] == [
        'security',
        'portability',
    ]


@pytest.mark.parametrize('custom_reviewer', [False, True])
def test_configured_runtime_defaults_do_not_create_run_option_conflicts(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
    custom_reviewer: bool,
) -> None:
    """Treat configured runtimes as the baseline for incompatible options."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[defaults]\n'
        'developer_runtime = "claude-code"\n'
        'reviewer_runtime = "claude-code"\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the accepted plan without starting an agent."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)
    options = () if custom_reviewer else ('--no-remediation',)
    reviewer = tmp_path / 'reviewer.py' if custom_reviewer else None

    assert main(run_arguments(enqueued_run, *options, reviewer=reviewer)) == 0

    plan = observed['plan']
    assert isinstance(plan, ReviewPlan)
    assert plan.developer_command == []


@pytest.mark.parametrize(
    'case',
    [
        (
            ('--reviewer-set', 'default', '--reviewer-agent', 'codex'),
            False,
            '--reviewer-set cannot be combined with single-reviewer options',
        ),
        (
            ('--no-remediation', '--developer-agent', 'codex'),
            False,
            'developer options cannot be combined with --no-remediation',
        ),
        (
            ('--reviewer-agent', 'codex'),
            True,
            'built-in reviewer options cannot be combined with a custom reviewer command',
        ),
        (
            ('--developer-agent', 'codex'),
            True,
            'built-in reviewer options cannot be combined with a custom reviewer command',
        ),
    ],
)
def test_run_option_conflicts_compare_against_configured_defaults(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[str, ...], bool, str],
) -> None:
    """Reject explicit runtime values that differ from configured defaults."""

    options, custom_reviewer, expected_message = case
    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[defaults]\n'
        'developer_runtime = "claude-code"\n'
        'reviewer_runtime = "claude-code"\n\n'
        '[reviewer_sets.default]\n'
        'members = [\n'
        '  { id = "security", runtime = "codex" },\n'
        '  { id = "portability", runtime = "claude-code" },\n'
        ']\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    reviewer = tmp_path / 'reviewer.py' if custom_reviewer else None

    assert main(run_arguments(enqueued_run, *options, reviewer=reviewer)) == 2

    assert expected_message in capsys.readouterr().err


def test_default_database_is_outside_a_repo_in_the_home_directory() -> None:
    """Keep default orchestration state outside a typical reviewed repo."""

    # This asserts the production location, so it deliberately uses the
    # module-level import rather than cli.DEFAULT_DATABASE: the session guard
    # redirects the attribute, and reading it here would assert a temporary path
    # and quietly stop checking anything.
    repo = Path.home() / 'Projects/repo'

    assert Path.home() / '.local/state/agent-orchestra/state.db' == DEFAULT_DATABASE
    assert not DEFAULT_DATABASE.is_relative_to(repo)


def test_run_records_requested_changes(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Advance a rejected review to the remediation boundary."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state is RunState.CHANGES_REQUESTED
    )


@pytest.mark.parametrize('iterations', ['1', '3'])
def test_review_only_run_stops_at_its_verdict(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    capsys: pytest.CaptureFixture[str],
    iterations: str,
) -> None:
    """End a run that cannot remediate at its review verdict, not at failure."""

    # A run with no developer command cannot spend the remediation budget, so
    # exhausting it must not turn a completed review into a failed job. This
    # failed only at --max-iterations 1, where the budget check preempted the
    # review-only return, which is how it escaped the suite.
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')

    result = main(
        run_arguments(enqueued_run, '--max-iterations', iterations, reviewer=reviewer)
    )

    assert result == 0
    # Schema 23 is where this stopped reporting failed and exit 2, so the
    # version is asserted literally alongside the behaviour it labels.
    assert json.loads(capsys.readouterr().out)['schema_version'] == 23
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state is RunState.CHANGES_REQUESTED
    )


def test_no_remediation_reviews_once_without_a_developer(
    tmp_path: Path, enqueued_run: CliRunContext, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review once through a built-in adapter and stop at the verdict."""

    reviewer = tmp_path / 'bin/codex'
    write_fake_codex(reviewer, mode='changes_requested')
    monkey_path = f'{reviewer.parent}{os.pathsep}{os.environ.get("PATH", "")}'
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PATH', monkey_path)
        codex_home = tmp_path / 'codex-home'
        skill = codex_home / 'skills/agent-orchestra-reviewer'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('review instructions\n')
        patch.setenv('CODEX_HOME', str(codex_home))
        result = main(
            run_arguments(
                enqueued_run,
                '--no-remediation',
                '--max-iterations',
                '1',
                '--reviewer-model',
                'test-model',
            )
        )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['state'] == 'changes_requested'
    assert document['error'] is None


@pytest.mark.parametrize(
    ('developer_runtime', 'reviewer_runtime'),
    [
        ('codex', 'codex'),
        ('codex', 'claude-code'),
        ('claude-code', 'codex'),
        ('claude-code', 'claude-code'),
    ],
)
def test_worker_remediates_and_reviews_new_digest(
    tmp_path: Path, developer_runtime: str, reviewer_runtime: str
) -> None:
    """Complete the same canonical loop for every runtime combination."""

    context = create_worker_run(tmp_path)
    digest = context.run.diff_digest
    assert digest is not None
    reviewer = tmp_path / f'{reviewer_runtime}-reviewer.py'
    developer = tmp_path / f'{developer_runtime}-developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer)

    result = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=2,
        ),
    )

    assert result.state.value == 'awaiting_commit_authorization'
    assert result.iteration == 2
    assert result.diff_digest != digest
    messages = evidence_directory(context) / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    integrity = json.loads(
        (evidence_directory(context) / '.integrity.json').read_text()
    )
    indexed_types = {
        entry['path']: entry['evidence_type'] for entry in integrity['entries']
    }
    assert {
        path: _canonical_evidence_type(path)
        for path in indexed_types
        if path.startswith('messages/')
    } == {
        path: evidence_type
        for path, evidence_type in indexed_types.items()
        if path.startswith('messages/')
    }
    assert indexed_types['messages/000004-developer-handoff.json'] == (
        'developer_handoff'
    )
    second_request = json.loads((messages / '000005-review-request.json').read_text())
    assert second_request['iteration'] == 2
    assert second_request['scope']['diff_digest'] == result.diff_digest


def test_resume_validation_required_continues_same_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-run blocked validation and approve the next digest under one run ID."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)

    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )

    assert blocked.id == context.run.id
    assert blocked.state is RunState.VALIDATION_REQUIRED
    messages = evidence_directory(context) / 'messages'
    assert (messages / '000004-developer-handoff.json').is_file()
    assert not (
        evidence_directory(context) / 'logs/000004-rejected-developer-handoff.json'
    ).exists()

    result = main(resume_arguments(context))

    assert result == 0
    resumed = context.store.get(context.run.id)
    assert resumed.id == context.run.id
    assert resumed.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert resumed.iteration == 2
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-remediation-request.json',
        '000006-developer-handoff.json',
        '000007-review-request.json',
        '000008-review-result.json',
    ]
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 23,
        'agent_orchestra_version': version('agent-orchestra'),
        'job_id': str(context.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }

    assert main(resume_arguments(context)) == 2
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['error']['code'] == 'job_not_resumable'
    assert repeated['error']['message'] == (
        'job is not resumable from awaiting_commit_authorization'
    )
    assert len(tuple(messages.iterdir())) == 8


def test_resume_retries_an_interrupted_validation_required_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retry an interrupted recovery developer request under the same run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            developer_timeout_seconds=1,
            max_iterations=3,
        ),
    )

    assert blocked.state is RunState.VALIDATION_REQUIRED
    developer.write_text('"""Slow developer."""\nimport time\ntime.sleep(5)\n')

    assert main(resume_arguments(context)) == 2
    interrupted = json.loads(capsys.readouterr().out)
    assert interrupted['error']['code'] == 'resume_interrupted'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED

    write_developer(developer)

    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    messages = evidence_directory(context) / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-remediation-request.json',
        '000006-developer-handoff.json',
        '000007-review-request.json',
        '000008-review-result.json',
    ]
    invocations = evidence_directory(context) / 'invocations'
    retry = json.loads((invocations / '000005-developer-attempt-0002.json').read_text())
    assert retry['attempt'] == 2
    assert retry['timed_out'] is False


def test_resume_archives_rejected_developer_handoff_by_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retain an attempt-qualified rejected handoff during validation recovery."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    write_developer(developer)
    configure_agent(developer, status='invalid')

    assert main(resume_arguments(context)) == 2

    assert json.loads(capsys.readouterr().out)['error']['code'] == (
        'resume_evidence_invalid'
    )
    rejected = (
        evidence_directory(context)
        / 'logs/000006-rejected-developer-handoff-attempt-0001.json'
    )
    assert rejected.is_file()


def test_resume_rejects_handoff_with_a_non_remediation_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed when a recoverable handoff replies to the wrong message type."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )

    messages = evidence_directory(context) / 'messages'
    review_result = json.loads((messages / '000002-review-result.json').read_text())
    handoff_path = messages / '000004-developer-handoff.json'
    handoff = json.loads(handoff_path.read_text())
    handoff['in_reply_to'] = review_result['message_id']
    handoff_path.write_text(json.dumps(handoff))

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED


def test_resume_rejects_handoff_linked_to_a_different_review_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a valid-looking chain whose blocked handoff uses stale review evidence."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )

    messages = evidence_directory(context) / 'messages'
    first_request = json.loads((messages / '000001-review-request.json').read_text())
    first_result = json.loads((messages / '000002-review-result.json').read_text())
    remediation = json.loads((messages / '000003-remediation-request.json').read_text())
    handoff = json.loads((messages / '000004-developer-handoff.json').read_text())

    extra_request = dict(first_request)
    extra_request.update(
        {'message_id': str(uuid4()), 'sequence': 5, 'in_reply_to': None}
    )
    extra_result = dict(first_result)
    extra_result.update(
        {
            'message_id': str(uuid4()),
            'in_reply_to': extra_request['message_id'],
            'sequence': 6,
        }
    )
    remediation.update({'message_id': str(uuid4()), 'sequence': 7})
    handoff.update(
        {
            'message_id': str(uuid4()),
            'in_reply_to': remediation['message_id'],
            'sequence': 8,
        }
    )

    (messages / '000005-review-request.json').write_text(json.dumps(extra_request))
    (messages / '000006-review-result.json').write_text(json.dumps(extra_result))
    (messages / '000007-remediation-request.json').write_text(json.dumps(remediation))
    (messages / '000008-developer-handoff.json').write_text(json.dumps(handoff))

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED


@pytest.mark.parametrize(
    'tamper',
    [
        'initial-prior-review-path',
        'repeat-prior-review-path',
        'result-artifact-path',
        'missing-result-artifact',
    ],
)
def test_resume_rejects_tampered_review_exchange_payload_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tamper: str
) -> None:
    """Fail closed when review evidence links do not describe the chain."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    configure_agent(reviewer, sleep_iteration=2, sleep_seconds=5)
    write_developer(developer)

    with pytest.raises(WorkerError, match='reviewer timed out'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=1,
                max_iterations=3,
            ),
        )

    messages = evidence_directory(context) / 'messages'
    first_request_path = messages / '000001-review-request.json'
    first_result_path = messages / '000002-review-result.json'
    repeat_request_path = messages / '000005-review-request.json'
    first_request = json.loads(first_request_path.read_text())
    first_result = json.loads(first_result_path.read_text())
    repeat_request = json.loads(repeat_request_path.read_text())
    if tamper == 'initial-prior-review-path':
        first_request['payload']['prior_review_path'] = str(first_result_path)
        first_request_path.write_text(json.dumps(first_request))
    elif tamper == 'repeat-prior-review-path':
        repeat_request['payload']['prior_review_path'] = str(first_request_path)
        repeat_request_path.write_text(json.dumps(repeat_request))
    elif tamper == 'result-artifact-path':
        first_result['payload']['artifact_path'] = str(first_request_path)
        first_result_path.write_text(json.dumps(first_result))
    else:
        Path(first_result['payload']['artifact_path']).unlink()

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED


@pytest.mark.parametrize(
    ('max_iterations', 'change_worktree', 'expected'),
    [(1, True, ITERATION_LIMIT), (2, False, NO_REMEDIATION_CHANGE)],
)
def test_worker_stops_bounded_non_progress(
    tmp_path: Path,
    max_iterations: int,
    change_worktree: bool,
    expected: str,
) -> None:
    """Fail durably on iteration exhaustion or a no-change handoff."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'loop-reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=change_worktree)

    with pytest.raises(WorkerError, match=expected):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                max_iterations=max_iterations,
            ),
        )

    assert context.store.get(context.run.id).state is RunState.FAILED
    failure = json.loads((evidence_directory(context) / 'failure.json').read_text())
    assert failure['run_id'] == context.run.id
    assert failure['state'] == 'failed'
    assert failure['error'] == {'code': 'worker_error', 'message': expected}


def test_worker_surfaces_developer_disagreement_for_human_decision(
    tmp_path: Path,
) -> None:
    """Preserve a justified no-change disagreement without failing the run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=False, disposition='rejected')

    result = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=2,
        ),
    )

    assert result.state.value == 'changes_requested'
    evidence = json.loads(
        (evidence_directory(context) / 'decision-required.json').read_text()
    )
    assert evidence['reason']['code'] == 'developer_disagreement'
    assert 'disputed every finding' in evidence['reason']['message']
    assert not (evidence_directory(context) / 'failure.json').exists()


def test_run_keeps_blocked_review_awaiting_resolution(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Persist a blocked review without inventing a terminal state."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'blocked')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING


@pytest.mark.parametrize(
    'reviewer_command', [['/missing/reviewer'], ['/usr/bin/false']]
)
def test_run_marks_reviewer_execution_failure(
    enqueued_run: CliRunContext,
    reviewer_command: list[str],
) -> None:
    """Persist failed state for missing and nonzero reviewer commands."""

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(enqueued_run.runs_directory),
            '--',
            *reviewer_command,
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED
    failure = json.loads(
        (evidence_directory(enqueued_run) / 'failure.json').read_text()
    )
    assert failure['error']['code'] == 'resume_execution_failed'


def test_run_marks_reviewer_timeout(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Persist interrupted state when the bounded reviewer exceeds its timeout."""

    reviewer = tmp_path / 'slow.py'
    reviewer.write_text('"""Slow test reviewer."""\nimport time\ntime.sleep(5)\n')

    result = main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer))

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    invocation = json.loads(
        (
            evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
        ).read_text()
    )
    assert invocation['exit_code'] is None
    assert invocation['timed_out'] is True


@pytest.mark.parametrize(
    'failure_case',
    [
        (
            'structured_output_exhausted',
            'claude-code exhausted structured-output retries: Failed after 5 attempts',
            'reviewer_structured_output_exhausted',
        ),
        (
            'turn_limit_exhausted',
            'claude-code exhausted its turn limit; num_turns=22',
            'reviewer_turn_limit_exhausted',
        ),
        (
            'provider_budget_exhausted',
            'claude-code exhausted its budget limit; total_cost_usd=0.48',
            'reviewer_provider_budget_exhausted',
        ),
    ],
)
def test_bounded_reviewer_failure_is_resumable_with_a_new_attempt(
    failure_case: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Preserve the failed attempt and retry its immutable request on resume."""

    failure_code, failure_message, workflow_code = failure_case
    reviewer = enqueued_run.repo.parent / 'reviewer.py'
    reviewer.write_text('"""Adapter boundary placeholder."""\n')
    attempts = 0

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Fail once with runtime metadata, then return canonical approval."""

        nonlocal attempts
        attempts += 1
        if request.on_started is not None:
            request.on_started()
        if attempts == 1:
            return AgentResult(
                succeeded=False,
                summary='',
                stdout=None,
                stderr='error: reviewer bound exhausted',
                exit_code=2,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        document = json.loads(request.request_path.read_text())
        artifact = Path(document['payload']['artifact_path'])
        artifact.write_text('# Review\n')
        request.response_path.write_text(
            json.dumps(
                {
                    'schema_version': 1,
                    'message_id': str(uuid4()),
                    'in_reply_to': document['message_id'],
                    'run_id': document['run_id'],
                    'sequence': document['sequence'] + 1,
                    'iteration': document['iteration'],
                    'message_type': 'review_result',
                    'sender': 'reviewer',
                    'recipient': 'orchestrator',
                    'created_at': '2026-09-16T20:00:00Z',
                    'scope': document['scope'],
                    'payload': {
                        'verdict': 'approved',
                        'summary': 'Ready.',
                        'findings': [],
                        'validation': [],
                        'verification_gaps': [],
                        'artifact_path': str(artifact),
                    },
                }
            )
        )
        return AgentResult(
            succeeded=True,
            summary='',
            stdout=None,
            stderr=None,
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)

    assert main(run_arguments(enqueued_run, reviewer=reviewer)) == 2
    failure_document = json.loads(capsys.readouterr().out)
    assert failure_document['error']['code'] == workflow_code
    assert failure_message in failure_document['error']['message']
    assert 'resume the interrupted job' in failure_document['error']['message']
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    persisted_failure = json.loads(
        (evidence_directory(enqueued_run) / 'failure.json').read_text()
    )
    assert persisted_failure['state'] == 'interrupted'
    assert persisted_failure['error']['code'] == workflow_code

    assert main(resume_arguments(enqueued_run)) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['state'] == 'awaiting_commit_authorization'
    invocations_directory = evidence_directory(enqueued_run) / 'invocations'
    first = json.loads((invocations_directory / '000001-reviewer.json').read_text())
    retry = json.loads(
        (invocations_directory / '000001-reviewer-attempt-0002.json').read_text()
    )
    assert first['attempt'] == 1
    assert first['conclusion'] == 'failed'
    assert retry['attempt'] == 2
    assert retry['conclusion'] == 'succeeded'


def test_provider_execution_failure_is_classified_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Expose a provider execution failure without offering an unsafe retry."""

    reviewer = enqueued_run.repo.parent / 'reviewer.py'
    reviewer.write_text('"""Adapter boundary placeholder."""\n')

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Return one classified non-bound provider failure."""

        if request.on_started is not None:
            request.on_started()
        return AgentResult(
            succeeded=False,
            summary='',
            stdout=None,
            stderr='error: provider execution failed',
            exit_code=2,
            failure_code='provider_execution_failed',
            failure_message='claude-code failed during execution',
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)

    assert main(run_arguments(enqueued_run, reviewer=reviewer)) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure['error'] == {
        'code': 'reviewer_provider_execution_failed',
        'message': 'claude-code failed during execution',
    }
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED


@pytest.mark.parametrize(
    'case',
    [
        ('approved', '30', 0, 'succeeded', None),
        ('nonzero', '30', 2, 'failed', 'codex exec failed with code 9'),
        ('timeout', '8', 2, 'timed_out', 'codex review timed out'),
    ],
)
def test_builtin_run_exposes_child_output_through_task_view(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
    case: tuple[str, str, int, str, str | None],
) -> None:
    """Retain built-in child streams across success, error, and timeout."""

    # The timeout case exercises the adapter's own bound on the child, not the
    # orchestrator's bound on the adapter. The adapter allows the child
    # timeout_seconds - 5, so --timeout 8 kills the child three seconds in while
    # the orchestrator is still waiting, and the child's twenty-second sleep
    # keeps it alive well past that deadline. Both margins have to stay wide:
    # equal deadlines make which bound fires a race, which is what made this
    # test flaky under CI load. test_run_marks_reviewer_timeout covers the
    # orchestrator killing an adapter that overruns, where nothing competes.
    # Either bound records timed_out: the adapter reports its own expiry through
    # the runtime metadata sidecar, so the two agree.
    mode, timeout, expected_result, conclusion, diagnostic = case
    fake_codex = tmp_path / 'bin/codex'
    write_fake_codex(fake_codex, mode=mode)
    current_path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', f'{fake_codex.parent}{os.pathsep}{current_path}')
    codex_home = tmp_path / 'codex-home'
    skill = codex_home / 'skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    result = main(
        run_arguments(
            enqueued_run,
            '--reviewer-model',
            'test-model',
            '--timeout',
            timeout,
        )
    )
    capsys.readouterr()

    assert result == expected_result
    assert (
        main(
            [
                '--database',
                str(enqueued_run.database),
                'task',
                f'{enqueued_run.run.id}:000001-reviewer',
                '--runs-directory',
                str(enqueued_run.runs_directory),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    attempt = document['task']['attempts'][0]
    assert 'child stdout\n' in attempt['streams']['stdout']['content']
    stderr = attempt['streams']['stderr']['content']
    assert 'child stderr\n' in stderr
    assert attempt['requested_model'] == 'test-model'
    assert attempt['effective_models'] == []
    assert attempt['effective_model_status'] == 'unavailable'
    if diagnostic is not None:
        assert diagnostic in stderr, (
            f'expected the adapter to report {diagnostic!r}; '
            f'the child ended for another reason. stderr: {stderr!r}'
        )
    assert attempt['conclusion'] == conclusion
    assert attempt['timed_out'] is (conclusion == 'timed_out')


def test_run_rejects_state_database_inside_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep mutable orchestration state outside the reviewed worktree."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = repo / '.agent-orchestra/state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = JobStore(database).list_runs()[0]

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
        ]
    )

    assert result == 2
    assert JobStore(database).get(run.id).state.value == 'queued'
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error'] == {
        'code': 'worker_error',
        'message': 'state database must be outside the worktree',
    }


def test_run_missing_database_is_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep an expected run lookup failure in its versioned JSON contract."""

    database = tmp_path / 'missing.db'

    assert (
        main(
            [
                '--database',
                str(database),
                'run',
                'job-1',
                '--objective',
                'Review the change.',
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'schema_version': 23,
        'agent_orchestra_version': version('agent-orchestra'),
        'job_id': 'job-1',
        'error': {
            'code': 'state_database_not_found',
            'message': f'state database not found: {database}',
        },
    }


def test_run_rejects_evidence_directory_inside_worktree(
    capsys: pytest.CaptureFixture[str], enqueued_run: CliRunContext
) -> None:
    """Keep mutable review messages and artifacts outside the reviewed worktree."""

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(enqueued_run.repo / 'runs'),
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.QUEUED
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error']['code'] == 'worker_error'
    assert 'evidence directory must be outside' in captured.out


def test_run_handles_digest_failure_before_transition(
    capsys: pytest.CaptureFixture[str], enqueued_run: CliRunContext
) -> None:
    """Report a disappeared repo without a traceback or state mutation."""

    shutil.rmtree(enqueued_run.repo / '.git')

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(enqueued_run.runs_directory),
            '--',
            '/usr/bin/true',
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.QUEUED
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error']['code'] == 'worker_error'
    assert 'cannot compute worktree digest' in captured.out


def test_run_marks_post_review_digest_failure(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Fail durably when Git state disappears during reviewer execution."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    with reviewer.open('a') as file:
        file.write('\nimport shutil\nshutil.rmtree(Path.cwd() / ".git")\n')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED
