"""Tests for normalized external agent invocation behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_orchestra.agents import CommandAgentAdapter, DeveloperRequest
from agent_orchestra.runtime_metadata import RUNTIME_METADATA_ENV


@pytest.mark.parametrize('failure', ['timeout', 'interrupt'])
def test_command_adapter_preserves_metadata_across_exceptional_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Consume reported provenance before propagating a failed invocation."""

    metadata_path = tmp_path / 'runtime.json'

    class FailedProcess:
        """Simulate a process that times out or is interrupted."""

        returncode = -9

        def __init__(self, _command: list[str], **kwargs: Any) -> None:
            """Write runtime metadata at process construction."""

            self.finished = False
            self.environment = kwargs['env']

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            """Raise once, then return drained streams after termination."""

            if self.finished:
                return '', ''
            self.finished = True
            if failure == 'timeout':
                command = 'command'
                raise subprocess.TimeoutExpired(command, timeout or 1)
            raise KeyboardInterrupt

        def kill(self) -> None:
            """Accept process termination."""

    def fail(command: list[str], **kwargs: Any) -> FailedProcess:
        """Write adapter metadata and return the failed process."""

        path = Path(kwargs['env'][RUNTIME_METADATA_ENV])
        path.write_text(
            json.dumps(
                {
                    'schema_version': 2,
                    'effective_models': ['reported-model'],
                    'status': 'reported',
                    'timed_out': False,
                }
            )
        )
        return FailedProcess(command, **kwargs)

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', fail)
    request = DeveloperRequest(
        objective='Test metadata recovery.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        runtime_metadata_path=metadata_path,
    )

    expected = subprocess.TimeoutExpired if failure == 'timeout' else KeyboardInterrupt
    with pytest.raises(expected) as raised:
        CommandAgentAdapter(('agent',)).execute(request)

    assert raised.value.__dict__['effective_models'] == ('reported-model',)
    assert raised.value.__dict__['effective_model_status'] == 'reported'
    assert not metadata_path.exists()


def test_command_adapter_reports_adapter_detected_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carry an adapter's own expired bound out of its non-zero exit."""

    metadata_path = tmp_path / 'runtime.json'

    class TimedOutProcess:
        """Stand in for an adapter that reported a timeout and exited two."""

        returncode = 2

        def __init__(self, _command: list[str], **kwargs: Any) -> None:
            """Record the adapter's timeout the way the real adapter does."""

            path = Path(kwargs['env'][RUNTIME_METADATA_ENV])
            path.write_text(
                json.dumps(
                    {
                        'schema_version': 2,
                        'effective_models': [],
                        'status': 'unavailable',
                        'timed_out': True,
                    }
                )
            )

        def communicate(self, **_: Any) -> tuple[str, str]:
            """Return the adapter's diagnostic without blocking."""

            return '', 'error: codex review timed out'

        def kill(self) -> None:
            """Accept process termination."""

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', TimedOutProcess)
    request = DeveloperRequest(
        objective='Test timeout reporting.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=30,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        runtime_metadata_path=metadata_path,
    )

    result = CommandAgentAdapter(('agent',)).execute(request)

    # The orchestrator never saw TimeoutExpired here; its own bound was 30s and
    # the adapter exited normally with code 2. Only the sidecar distinguishes
    # this from a crash.
    assert result.exit_code == 2
    assert result.succeeded is False
    assert result.timed_out is True
    assert not metadata_path.exists()


def test_command_adapter_ignores_non_utf8_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete the invocation when optional runtime metadata cannot be decoded."""

    metadata_path = tmp_path / 'runtime.json'

    class SuccessfulProcess:
        """Simulate a successful captured process."""

        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            """Return captured process streams."""

            del timeout
            return 'done', ''

    def succeed(_command: list[str], **kwargs: Any) -> SuccessfulProcess:
        """Write malformed metadata and return a successful process."""

        Path(kwargs['env'][RUNTIME_METADATA_ENV]).write_bytes(b'\xff')
        return SuccessfulProcess()

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', succeed)
    request = DeveloperRequest(
        objective='Test malformed metadata recovery.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        runtime_metadata_path=metadata_path,
    )

    result = CommandAgentAdapter(('agent',)).execute(request)

    assert result.succeeded is True
    assert result.stdout == 'done'
    assert result.effective_models == ()
    assert result.effective_model_status == 'unavailable'
    assert not metadata_path.exists()


def test_command_adapter_marks_running_only_after_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist activation after process creation and before waiting for output."""

    events: list[str] = []

    class SuccessfulProcess:
        """Record the observable process lifecycle."""

        returncode = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            """Record that process waiting followed activation persistence."""

            del timeout
            events.append('communicate')
            return '', ''

    def spawn(_command: list[str], **_kwargs: Any) -> SuccessfulProcess:
        """Represent successful operating-system process activation."""

        events.append('spawn')
        return SuccessfulProcess()

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', spawn)
    request = DeveloperRequest(
        objective='Test activation ordering.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        on_started=lambda: events.append('running'),
    )

    CommandAgentAdapter(('agent',)).execute(request)

    assert events == ['spawn', 'running', 'communicate']


def test_command_adapter_does_not_mark_running_when_spawn_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep activation failure on the direct pending-to-failed edge."""

    started = False

    def fail_spawn(_command: list[str], **_kwargs: Any) -> None:
        """Represent an executable that the operating system cannot start."""

        message = 'missing executable'
        raise OSError(message)

    def mark_started() -> None:
        """Record an incorrect running transition if called."""

        nonlocal started
        started = True

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', fail_spawn)
    request = DeveloperRequest(
        objective='Test failed activation.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        on_started=mark_started,
    )

    with pytest.raises(OSError, match='missing executable'):
        CommandAgentAdapter(('agent',)).execute(request)

    assert started is False


def test_command_adapter_does_not_mark_running_when_spawn_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave activation uncertain when interruption occurs during process creation."""

    started = False

    def interrupt_spawn(_command: list[str], **_kwargs: Any) -> None:
        """Represent an interruption before a child handle is returned."""

        raise KeyboardInterrupt

    def mark_started() -> None:
        """Record an incorrect running transition if called."""

        nonlocal started
        started = True

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', interrupt_spawn)
    request = DeveloperRequest(
        objective='Test interrupted activation.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        on_started=mark_started,
    )

    with pytest.raises(KeyboardInterrupt):
        CommandAgentAdapter(('agent',)).execute(request)

    assert started is False


@pytest.mark.parametrize('redirected', [False, True])
def test_command_adapter_reaps_child_when_started_callback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirected: bool,
) -> None:
    """Terminate a spawned child when activation evidence cannot be persisted."""

    events: list[str] = []

    class ActiveProcess:
        """Record cleanup performed after the lifecycle callback fails."""

        returncode = -9

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            """Record captured-process cleanup."""

            del timeout
            events.append('communicate')
            return '', ''

        def wait(self, timeout: int | None = None) -> int:
            """Record redirected-process cleanup."""

            del timeout
            events.append('wait')
            return self.returncode

        def kill(self) -> None:
            """Record child termination."""

            events.append('kill')

    def spawn(_command: list[str], **_kwargs: Any) -> ActiveProcess:
        """Return one active child process."""

        events.append('spawn')
        return ActiveProcess()

    def fail_started() -> None:
        """Represent failure to persist the running state."""

        events.append('running')
        message = 'cannot persist running state'
        raise OSError(message)

    monkeypatch.setattr('agent_orchestra.agents.subprocess.Popen', spawn)
    request = DeveloperRequest(
        objective='Test failed lifecycle persistence.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        stdout_path=tmp_path / 'stdout.log' if redirected else None,
        stderr_path=tmp_path / 'stderr.log' if redirected else None,
        on_started=fail_started,
    )

    with pytest.raises(OSError, match='cannot persist running state'):
        CommandAgentAdapter(('agent',)).execute(request)

    cleanup = 'wait' if redirected else 'communicate'
    assert events == ['spawn', 'running', 'kill', cleanup]
