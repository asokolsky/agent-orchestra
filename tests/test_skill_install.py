"""Tests for installing bundled agent skills."""

from typing import TYPE_CHECKING

import pytest

from agent_orchestra.skill_install import (
    AgentTarget,
    SkillInstallError,
    install_skills,
)

if TYPE_CHECKING:
    from pathlib import Path


def create_skill(root: Path, name: str, body: str = 'instructions\n') -> Path:
    """Create a minimal skill directory for an installer test."""

    skill = root / name
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(body)
    (skill / 'SKILL-meta.md').write_text('metadata\n')
    return skill


def test_install_for_both_agents_is_idempotent(tmp_path: Path) -> None:
    """Install identical copies for both agents and safely repeat the operation."""

    source = tmp_path / 'source'
    create_skill(source, 'example-skill')
    codex_home = tmp_path / 'codex'
    claude_home = tmp_path / 'claude'
    agents = (AgentTarget.CODEX, AgentTarget.CLAUDE_CODE)

    installed = install_skills(
        ('example-skill',),
        agents,
        source_root=source,
        codex_home=codex_home,
        claude_home=claude_home,
    )
    repeated = install_skills(
        ('example-skill',),
        agents,
        source_root=source,
        codex_home=codex_home,
        claude_home=claude_home,
    )

    assert all(result.installed for result in installed)
    assert not any(result.installed for result in repeated)
    assert (codex_home / 'skills/example-skill/SKILL.md').is_file()
    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()


def test_unchanged_managed_skill_is_upgraded(tmp_path: Path) -> None:
    """Upgrade a managed destination when its bundled source changes."""

    source = tmp_path / 'source'
    skill = create_skill(source, 'example-skill', 'version 1\n')
    codex_home = tmp_path / 'codex'
    install_skills(
        ('example-skill',),
        (AgentTarget.CODEX,),
        source_root=source,
        codex_home=codex_home,
    )
    (skill / 'SKILL.md').write_text('version 2\n')

    upgraded = install_skills(
        ('example-skill',),
        (AgentTarget.CODEX,),
        source_root=source,
        codex_home=codex_home,
    )

    destination = codex_home / 'skills/example-skill/SKILL.md'
    assert destination.read_text() == 'version 2\n'
    assert upgraded[0].installed


def test_locally_modified_managed_skill_is_not_upgraded(tmp_path: Path) -> None:
    """Preserve local edits even when a newer bundled source is available."""

    source = tmp_path / 'source'
    skill = create_skill(source, 'example-skill', 'version 1\n')
    codex_home = tmp_path / 'codex'
    install_skills(
        ('example-skill',),
        (AgentTarget.CODEX,),
        source_root=source,
        codex_home=codex_home,
    )
    destination = codex_home / 'skills/example-skill/SKILL.md'
    destination.write_text('local edit\n')
    (skill / 'SKILL.md').write_text('version 2\n')

    with pytest.raises(SkillInstallError, match='locally modified'):
        install_skills(
            ('example-skill',),
            (AgentTarget.CODEX,),
            source_root=source,
            codex_home=codex_home,
        )

    assert destination.read_text() == 'local edit\n'


def test_modified_destination_blocks_all_installation(tmp_path: Path) -> None:
    """Refuse the complete request before changing another agent destination."""

    source = tmp_path / 'source'
    create_skill(source, 'example-skill')
    codex_home = tmp_path / 'codex'
    modified = create_skill(codex_home / 'skills', 'example-skill', 'modified\n')
    claude_home = tmp_path / 'claude'

    with pytest.raises(SkillInstallError, match='locally modified'):
        install_skills(
            ('example-skill',),
            (AgentTarget.CODEX, AgentTarget.CLAUDE_CODE),
            source_root=source,
            codex_home=codex_home,
            claude_home=claude_home,
        )

    assert modified.is_dir()
    assert not (claude_home / 'skills/example-skill').exists()


def test_rejects_unknown_skill(tmp_path: Path) -> None:
    """Reject a skill name absent from the canonical source."""

    source = tmp_path / 'source'
    source.mkdir()

    with pytest.raises(SkillInstallError, match='skill not found'):
        install_skills(
            ('missing',),
            (AgentTarget.CODEX,),
            source_root=source,
            codex_home=tmp_path / 'codex',
        )


def test_claude_config_dir_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honor Claude Code's configured home when no CLI override is supplied."""

    source = tmp_path / 'source'
    create_skill(source, 'example-skill')
    claude_home = tmp_path / 'configured-claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(claude_home))

    install_skills(
        ('example-skill',),
        (AgentTarget.CLAUDE_CODE,),
        source_root=source,
    )

    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()
