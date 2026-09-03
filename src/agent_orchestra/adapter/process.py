"""Run child agent processes while teeing their output to durable wrapper logs."""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class StreamingProcessResult:
    """Captured result from a process whose output was also streamed live."""

    returncode: int
    stdout: str
    stderr: str


def _write_all(sink: BinaryIO, content: bytes) -> None:
    """Write every byte to a blocking binary stream."""

    remaining = memoryview(content)
    while remaining:
        written = sink.write(remaining)
        if written is None or written <= 0:
            message = 'binary stream did not accept output'
            raise OSError(message)
        remaining = remaining[written:]


def _tee_stream(source: BinaryIO, sink: BinaryIO, chunks: list[bytes]) -> None:
    """Copy raw pipe bytes to a wrapper stream while retaining them for parsing."""

    while chunk := source.read(64 * 1024):
        chunks.append(chunk)
        try:
            _write_all(sink, chunk)
            sink.flush()
        except OSError, ValueError:
            # Keep draining the child pipe so a closed display sink cannot deadlock it.
            continue


def _write_stdin(sink: BinaryIO, content: bytes) -> None:
    """Write and close child stdin without blocking the timeout controller."""

    try:
        _write_all(sink, content)
        sink.flush()
    except BrokenPipeError:
        pass
    finally:
        with suppress(OSError, ValueError):
            sink.close()


def run_streaming_process(
    command: Sequence[str],
    *,
    input: str,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> StreamingProcessResult:
    """Run a bounded process, tee raw streams live, and retain decoded text."""

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        message = 'failed to create child process pipes'
        raise OSError(message)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    threads = (
        Thread(
            target=_tee_stream,
            args=(process.stdout, sys.stdout.buffer, stdout_chunks),
            daemon=True,
        ),
        Thread(
            target=_tee_stream,
            args=(process.stderr, sys.stderr.buffer, stderr_chunks),
            daemon=True,
        ),
        Thread(
            target=_write_stdin,
            args=(process.stdin, input.encode()),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join()
        raise subprocess.TimeoutExpired(
            list(command),
            timeout,
            output=b''.join(stdout_chunks).decode(errors='replace'),
            stderr=b''.join(stderr_chunks).decode(errors='replace'),
        ) from error
    except BaseException:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join()
        raise

    for thread in threads:
        thread.join()
    return StreamingProcessResult(
        returncode=returncode,
        stdout=b''.join(stdout_chunks).decode(errors='replace'),
        stderr=b''.join(stderr_chunks).decode(errors='replace'),
    )
