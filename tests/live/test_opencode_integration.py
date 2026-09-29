"""Opt-in lifecycle checks for the pinned and authenticated OpenCode CLI."""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.adapter.opencode_isolation import OPENCODE_SUPPORTED_VERSION
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
    from pathlib import Path

LIVE_OPENCODE: LiveRuntime = live_runtime('opencode')
LIVE_TIMEOUT_SECONDS = 300
SKIP_REASON = 'set AGENT_ORCHESTRA_LIVE_OPENCODE=1 to run live OpenCode checks'

pytestmark = [
    pytest.mark.live_opencode,
    pytest.mark.skipif(
        os.environ.get(LIVE_OPENCODE.opt_in_environment) != '1',
        reason=SKIP_REASON,
    ),
]


@pytest.fixture(scope='module')
def opencode_preflight() -> str:
    """Require the pinned CLI and both installed canonical role skills."""

    assert_runtime_capabilities(LIVE_OPENCODE)
    executable = shutil.which(LIVE_OPENCODE.executable)
    if executable is None:
        pytest.fail('opencode executable not found on PATH')
    version = run_command([executable, '--version'], timeout=30)
    require_success(version, context='opencode --version')
    if version.stdout.strip() != OPENCODE_SUPPORTED_VERSION:
        pytest.fail(f'OpenCode {OPENCODE_SUPPORTED_VERSION} is required')
    if LIVE_OPENCODE.requested_model is None:
        pytest.fail('set AGENT_ORCHESTRA_LIVE_OPENCODE_MODEL=provider/model')
    missing = [
        name
        for name in LIVE_OPENCODE.skill_names
        if not (skill_destination('opencode', name) / 'SKILL.md').is_file()
    ]
    if missing:
        pytest.fail(
            'install the OpenCode role skills: agent-orchestra skills install '
            '--agent opencode --skill agent-orchestra-reviewer '
            '--skill agent-orchestra-developer'
        )
    return executable


def test_live_opencode_local_review_and_remediation(
    tmp_path: Path, opencode_preflight: str
) -> None:
    """Find, remediate, and approve a deterministic temporary regression."""

    del opencode_preflight
    scenario = run_local_scenario(
        tmp_path, LIVE_OPENCODE, attempt_timeout=LIVE_TIMEOUT_SECONDS
    )
    assert_local_scenario(scenario, LIVE_OPENCODE)


def test_live_opencode_issue_review_uses_local_snapshot(
    tmp_path: Path,
    opencode_preflight: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review an immutable issue snapshot with no provider write."""

    del opencode_preflight
    assert_issue_scenario(
        tmp_path,
        LIVE_OPENCODE,
        attempt_timeout=LIVE_TIMEOUT_SECONDS,
        monkeypatch=monkeypatch,
    )
