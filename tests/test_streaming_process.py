"""Tests for live child-process output capture."""

from __future__ import annotations

import subprocess
import sys

import pytest

from agent_orchestra.adapter.process import run_streaming_process


def test_streaming_process_tees_and_retains_both_streams(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Expose child output live while retaining the same complete text."""

    result = run_streaming_process(
        [
            sys.executable,
            '-c',
            (
                'import sys; '
                'print("child stdout", flush=True); '
                'print("child stderr", file=sys.stderr, flush=True)'
            ),
        ],
        input='',
        timeout=5,
    )

    captured = capfd.readouterr()
    assert result.returncode == 0
    assert result.stdout == 'child stdout\n'
    assert result.stderr == 'child stderr\n'
    assert captured.out == result.stdout
    assert captured.err == result.stderr


def test_streaming_process_preserves_partial_output_on_timeout(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Drain and expose output written before a timed-out child is killed."""

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_streaming_process(
            [
                sys.executable,
                '-c',
                (
                    'import sys, time; '
                    'print("partial stdout", flush=True); '
                    'print("partial stderr", file=sys.stderr, flush=True); '
                    'time.sleep(5)'
                ),
            ],
            input='',
            timeout=0.1,
        )

    captured = capfd.readouterr()
    stdout = raised.value.output
    stderr = raised.value.stderr
    assert isinstance(stdout, str)
    assert isinstance(stderr, str)
    assert stdout == 'partial stdout\n'
    assert stderr == 'partial stderr\n'
    assert captured.out == stdout
    assert captured.err == stderr


def test_streaming_process_preserves_invalid_utf8_bytes(
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    """Tee raw bytes exactly while decoding retained output permissively."""

    raw_output = b'before\ncaf\xe9\nafter\n'
    result = run_streaming_process(
        [
            sys.executable,
            '-c',
            f'import sys; sys.stdout.buffer.write({raw_output!r})',
        ],
        input='',
        timeout=5,
    )

    captured = capfdbinary.readouterr()
    assert result.returncode == 0
    assert result.stdout == raw_output.decode(errors='replace')
    assert captured.out == raw_output


def test_streaming_process_times_out_while_child_ignores_stdin() -> None:
    """Apply the invocation deadline while a large stdin write is blocked."""

    with pytest.raises(subprocess.TimeoutExpired):
        run_streaming_process(
            [sys.executable, '-c', 'import time; time.sleep(5)'],
            input='x' * (1024 * 1024),
            timeout=0.1,
        )


def test_streaming_process_writes_complete_large_stdin() -> None:
    """Deliver a prompt larger than the pipe buffer without truncation."""

    content = 'x' * (1024 * 1024)
    result = run_streaming_process(
        [
            sys.executable,
            '-c',
            'import sys; print(len(sys.stdin.buffer.read()))',
        ],
        input=content,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == f'{len(content)}\n'
