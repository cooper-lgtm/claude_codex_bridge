from __future__ import annotations

import json
import os
from pathlib import Path

from storage.paths import PathLayout
from storage.path_helpers import RuntimeStatePlacement
from storage_classification import summarize_storage


def _write(path: Path, text: str = 'x') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _records_by_suffix(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item['relative_path']): item for item in payload['entries']}


def test_storage_classification_keeps_provider_authority_and_cache_separate(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    ccb = project_root / '.ccb'
    codex_home = ccb / 'agents' / 'agent1' / 'provider-state' / 'codex' / 'home'
    claude_home = ccb / 'agents' / 'agent2' / 'provider-state' / 'claude' / 'home'
    gemini_home = ccb / 'agents' / 'agent3' / 'provider-state' / 'gemini' / 'home'
    opencode_state = ccb / 'agents' / 'agent4' / 'provider-state' / 'opencode'

    _write(ccb / 'ccb.config', 'agent1:codex\n')
    _write(ccb / 'ccb_memory.md', '# shared memory\n')
    _write(ccb / 'history' / 'handoff.md', '# handoff\n')
    _write(ccb / 'workspaces' / 'agent1' / 'notes.txt', 'workspace change\n')
    _write(ccb / 'shared-cache' / 'claude' / 'versions' / '2.1.137' / 'claude', 'shared bin\n')
    _write(ccb / 'agents' / 'agent1' / 'runtime.json', '{}\n')
    _write(ccb / 'agents' / 'agent1' / 'memory.md', '# private memory\n')
    _write(ccb / 'state' / 'memory.seed.json', '{}\n')
    _write(ccb / 'runtime' / 'memory' / 'agent1.md', '# memory\n')
    _write(codex_home / 'sessions' / '2026' / 'session.jsonl')
    _write(codex_home / '.ccb-session-namespace.json', '{}\n')
    _write(codex_home / 'auth.json', '{}\n')
    _write(codex_home / 'config.toml', '# config\n')
    _write(codex_home / '.tmp' / 'plugins' / 'plugins' / 'demo' / 'SKILL.md')
    _write(codex_home / '.tmp' / 'plugins.sha', 'abc\n')
    source_skills = tmp_path / 'source-codex-home' / 'skills'
    source_skills.mkdir(parents=True, exist_ok=True)
    if hasattr(os, 'symlink'):
        os.symlink(source_skills, codex_home / 'skills')
        _write(
            codex_home / 'skills.ccb-projection.json',
            json.dumps(
                {
                    'record_type': 'ccb_projected_asset',
                    'label': 'codex-inherited-skills',
                    'source': str(source_skills),
                }
            )
            + '\n',
        )

    _write(claude_home / '.claude.json', '{}\n')
    _write(claude_home / '.claude' / '.credentials.json', '{}\n')
    _write(claude_home / '.config' / 'claude-code' / 'auth.json', '{}\n')
    _write(claude_home / '.claude' / 'settings.json', '{}\n')
    _write(claude_home / '.local' / 'share' / 'claude' / 'versions' / '2.1.137' / 'claude', 'bin\n')
    if hasattr(os, 'symlink'):
        (claude_home / '.local' / 'bin').mkdir(parents=True, exist_ok=True)
        os.symlink('../share/claude/versions/2.1.137/claude', claude_home / '.local' / 'bin' / 'claude')

    _write(gemini_home / '.gemini' / 'tmp' / 'checkpoint.json', '{}\n')
    _write(gemini_home / '.gemini' / 'oauth_creds.json', '{}\n')
    _write(gemini_home / '.gemini' / 'settings.json', '{}\n')
    _write(gemini_home / '.npm' / '_cacache' / 'content-v2' / 'sha512' / 'aa' / 'blob')
    _write(opencode_state / 'opencode.json', '{}\n')

    payload = summarize_storage(PathLayout(project_root))
    records = _records_by_suffix(payload)

    assert payload['shared_cache_root'] == str(ccb / 'shared-cache')
    assert payload['shared_cache_root_usable'] is True
    assert payload['shared_cache_status'] == 'enabled'
    assert payload['shared_cache_reason'] == 'enabled'
    assert records['agents/agent1/runtime.json']['storage_class'] == 'authority'
    assert records['agents/agent1/memory.md']['storage_class'] == 'user_content'
    assert records['agents/agent1/memory.md']['reason'] == 'agent_private_memory'
    assert records['ccb_memory.md']['storage_class'] == 'user_content'
    assert records['ccb_memory.md']['reason'] == 'project_shared_memory'
    assert records['state/memory.seed.json']['storage_class'] == 'authority'
    assert records['state/memory.seed.json']['reason'] == 'project_memory_seed'
    assert records['runtime/memory/agent1.md']['storage_class'] == 'runtime_ephemeral'
    assert records['runtime/memory/agent1.md']['reason'] == 'project_memory_bundle'
    assert records['history/handoff.md']['storage_class'] == 'user_content'
    assert records['workspaces/agent1/notes.txt']['storage_class'] == 'workspace'
    assert records['shared-cache/claude/versions/2.1.137/claude']['storage_class'] == 'rebuildable_cache'
    assert records['shared-cache/claude/versions/2.1.137/claude']['provider'] == 'claude'
    assert records['shared-cache/claude/versions/2.1.137/claude']['reason'] == 'shared_cache'
    assert records['agents/agent1/provider-state/codex/home/sessions/2026/session.jsonl']['storage_class'] == 'session'
    assert records['agents/agent1/provider-state/codex/home/.ccb-session-namespace.json']['storage_class'] == 'session'
    assert records['agents/agent1/provider-state/codex/home/auth.json']['storage_class'] == 'secret'
    assert records['agents/agent1/provider-state/codex/home/config.toml']['storage_class'] == 'projected_config'
    if hasattr(os, 'symlink'):
        assert records['agents/agent1/provider-state/codex/home/skills']['storage_class'] == 'projected_config'
    assert (
        records['agents/agent1/provider-state/codex/home/.tmp/plugins/plugins/demo/SKILL.md']['storage_class']
        == 'startup_authority_bundle'
    )
    assert records['agents/agent1/provider-state/codex/home/.tmp/plugins.sha']['storage_class'] == 'startup_authority_bundle'

    assert records['agents/agent2/provider-state/claude/home/.claude.json']['storage_class'] == 'session'
    assert records['agents/agent2/provider-state/claude/home/.claude/.credentials.json']['storage_class'] == 'secret'
    assert records['agents/agent2/provider-state/claude/home/.config/claude-code/auth.json']['storage_class'] == 'secret'
    assert records['agents/agent2/provider-state/claude/home/.claude/settings.json']['storage_class'] == 'projected_config'
    assert (
        records['agents/agent2/provider-state/claude/home/.local/share/claude/versions/2.1.137/claude']['storage_class']
        == 'rebuildable_cache'
    )
    assert records['agents/agent2/provider-state/claude/home/.local/share/claude/versions/2.1.137/claude']['active'] is False
    assert (
        records['agents/agent2/provider-state/claude/home/.local/share/claude/versions/2.1.137/claude'][
            'is_active_version'
        ]
        is True
    )
    assert (
        records['agents/agent2/provider-state/claude/home/.local/share/claude/versions/2.1.137/claude'][
            'reachable_from_current_symlink'
        ]
        is True
    )
    assert records['agents/agent2/provider-state/claude/home/.local/bin/claude']['active'] is True
    assert records['agents/agent2/provider-state/claude/home/.local/bin/claude']['is_active_version'] is False

    assert records['agents/agent3/provider-state/gemini/home/.gemini/tmp/checkpoint.json']['storage_class'] == 'session'
    assert records['agents/agent3/provider-state/gemini/home/.gemini/oauth_creds.json']['storage_class'] == 'secret'
    assert records['agents/agent3/provider-state/gemini/home/.gemini/settings.json']['storage_class'] == 'projected_config'
    assert (
        records['agents/agent3/provider-state/gemini/home/.npm/_cacache/content-v2/sha512/aa/blob']['storage_class']
        == 'rebuildable_cache'
    )
    assert records['agents/agent4/provider-state/opencode/opencode.json']['storage_class'] == 'projected_config'


def test_storage_classification_surfaces_profile_backed_runtime_home(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    profile_home = project_root / '.ccb' / 'provider-profiles' / 'agent2' / 'codex'
    _write(profile_home / 'sessions' / '2026' / 'session.jsonl')
    _write(profile_home / 'auth.json', '{}\n')
    _write(profile_home / '.tmp' / 'plugins' / 'plugins' / 'demo' / 'SKILL.md')

    payload = summarize_storage(PathLayout(project_root))
    records = _records_by_suffix(payload)

    assert records['provider-profiles/agent2/codex/sessions/2026/session.jsonl']['storage_class'] == 'session'
    assert records['provider-profiles/agent2/codex/auth.json']['storage_class'] == 'secret'
    assert (
        records['provider-profiles/agent2/codex/.tmp/plugins/plugins/demo/SKILL.md']['storage_class']
        == 'startup_authority_bundle'
    )


def test_path_layout_exposes_provider_shared_cache_under_runtime_state_root(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)

    assert layout.shared_cache_dir == layout.runtime_state_root / 'shared-cache'
    assert layout.provider_shared_cache_dir('claude') == layout.shared_cache_dir / 'claude'


def test_path_layout_ensures_provider_shared_cache_manifest(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)

    cache_dir = layout.ensure_provider_shared_cache_dir('claude', created_at='2026-05-11T00:00:00Z')
    manifest = json.loads((cache_dir / 'MANIFEST.json').read_text(encoding='utf-8'))

    assert cache_dir == layout.shared_cache_dir / 'claude'
    assert manifest['record_type'] == 'ccb_shared_cache_manifest'
    assert manifest['provider'] == 'claude'
    assert manifest['project_id'] == layout.project_id
    assert manifest['runtime_state_root'] == str(layout.runtime_state_root)
    assert manifest['entries'] == []


def test_path_layout_ensures_provider_external_cache_manifest(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / 'repo'
    xdg_cache = tmp_path / 'xdg-cache'
    monkeypatch.setenv('XDG_CACHE_HOME', str(xdg_cache))
    layout = PathLayout(project_root)

    cache_dir = layout.ensure_provider_external_cache_dir('claude', created_at='2026-05-13T00:00:00Z')
    manifest = json.loads((cache_dir / 'MANIFEST.json').read_text(encoding='utf-8'))

    assert cache_dir == xdg_cache / 'ccb' / 'projects' / layout.project_id[:16] / 'provider-cache' / 'claude'
    assert manifest['record_type'] == 'ccb_external_provider_cache_manifest'
    assert manifest['provider'] == 'claude'
    assert manifest['project_id'] == layout.project_id
    assert manifest['project_root'] == str(layout.project_root)


def test_path_layout_rejects_noncanonical_shared_cache_provider(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')

    try:
        layout.provider_shared_cache_dir('Claude Code')
    except ValueError as exc:
        assert 'provider must be one of' in str(exc)
    else:
        raise AssertionError('expected noncanonical provider to be rejected')


def test_storage_summary_hides_shared_cache_root_when_drvfs_is_not_relocated(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    layout.ccb_dir.mkdir(parents=True, exist_ok=True)
    object.__setattr__(
        layout,
        '_runtime_state_placement',
        RuntimeStatePlacement(
            anchor_path=layout.ccb_dir,
            effective_path=layout.ccb_dir,
            root_kind='project',
            relocation_reason=None,
            filesystem_hint='wsl_drvfs',
        ),
    )
    object.__setattr__(layout, '_state_root', layout.ccb_dir)

    payload = summarize_storage(layout)

    assert payload['shared_cache_root'] is None
    assert payload['shared_cache_root_usable'] is False
    assert payload['shared_cache_reason'] == 'wsl_drvfs_requires_runtime_relocation'


def test_path_layout_refuses_to_create_shared_cache_on_drvfs_without_relocation(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    layout.ccb_dir.mkdir(parents=True, exist_ok=True)
    object.__setattr__(
        layout,
        '_runtime_state_placement',
        RuntimeStatePlacement(
            anchor_path=layout.ccb_dir,
            effective_path=layout.ccb_dir,
            root_kind='project',
            relocation_reason=None,
            filesystem_hint='wsl_drvfs',
        ),
    )
    object.__setattr__(layout, '_state_root', layout.ccb_dir)

    try:
        layout.ensure_provider_shared_cache_dir('claude')
    except RuntimeError as exc:
        assert 'requires relocated runtime state' in str(exc)
    else:
        raise AssertionError('expected unsafe drvfs shared-cache creation to fail')
