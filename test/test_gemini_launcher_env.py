from __future__ import annotations

import json
import shlex

from agents.models import PermissionMode, QueuePolicy, RestoreMode, RuntimeMode, WorkspaceMode
from cli.models import ParsedStartCommand
from provider_backends.gemini import launcher as gemini_launcher
from provider_backends.gemini.launcher_runtime.env import build_gemini_env_prefix
from provider_backends.gemini.launcher_runtime.home import (
    prepare_gemini_home_overrides,
    resolve_gemini_home_layout,
)
from provider_profiles import ResolvedProviderProfile
from agents.models import AgentSpec
from storage.paths import PathLayout


def test_build_gemini_env_prefix_clears_non_inherited_api_and_exports_filtered_keys() -> None:
    profile = ResolvedProviderProfile(
        provider="gemini",
        agent_name="agent1",
        env={
            "GEMINI_API_KEY": "profile-key",
            "GEMINI_MODEL": "gemini-3.1-pro-preview",
            "GOOGLE_GEMINI_BASE_URL": "https://chatapi.onechats.ai",
            "OTHER_ENV": "ignored",
        },
        inherit_api=False,
    )

    prefix = build_gemini_env_prefix(
        profile=profile,
        extra_env={"GOOGLE_API_KEY": "extra-key", "UNRELATED": "ignored"},
    )

    assert "unset GEMINI_API_KEY" in prefix
    assert "unset GEMINI_MODEL" in prefix
    assert "unset GOOGLE_API_KEY" in prefix
    assert "unset GOOGLE_GEMINI_BASE_URL" in prefix
    assert "OTHER_ENV" not in prefix
    assert "UNRELATED" not in prefix
    assert (
        "export GEMINI_API_KEY=profile-key GEMINI_MODEL=gemini-3.1-pro-preview "
        "GOOGLE_API_KEY=extra-key GOOGLE_GEMINI_BASE_URL=https://chatapi.onechats.ai"
    ) in prefix


def _spec(name: str = 'agent1') -> AgentSpec:
    return AgentSpec(
        name=name,
        provider='gemini',
        target='.',
        workspace_mode=WorkspaceMode.GIT_WORKTREE,
        workspace_root=None,
        runtime_mode=RuntimeMode.PANE_BACKED,
        restore_default=RestoreMode.AUTO,
        permission_default=PermissionMode.MANUAL,
        queue_policy=QueuePolicy.SERIAL_PER_AGENT,
    )


def _prepared(runtime_dir) -> dict[str, object]:
    return {'project_root': runtime_dir}


def test_gemini_launcher_build_start_cmd_exports_managed_home(tmp_path) -> None:
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    spec = _spec()
    command = ParsedStartCommand(project=None, agent_names=('agent1',), restore=False, auto_permission=False)

    start_cmd = gemini_launcher.build_start_cmd(
        command,
        spec,
        runtime_dir,
        'gemini-sess-home',
        prepared_state=_prepared(runtime_dir),
    )

    expected_home = runtime_dir / 'gemini-home'
    expected_root = expected_home / '.gemini' / 'tmp'
    assert f'HOME={shlex.quote(str(expected_home))}' in start_cmd
    assert f'GEMINI_CLI_HOME={shlex.quote(str(expected_home))}' in start_cmd
    assert f'GEMINI_ROOT={shlex.quote(str(expected_root))}' in start_cmd


def test_gemini_launcher_build_start_cmd_uses_agent_provider_state_home_for_managed_runtime(tmp_path) -> None:
    runtime_dir = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-runtime' / 'gemini'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    spec = _spec()
    command = ParsedStartCommand(project=None, agent_names=('agent1',), restore=False, auto_permission=False)

    start_cmd = gemini_launcher.build_start_cmd(
        command,
        spec,
        runtime_dir,
        'gemini-sess-home',
        prepared_state=_prepared(runtime_dir),
    )

    expected_home = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-state' / 'gemini' / 'home'
    expected_root = expected_home / '.gemini' / 'tmp'
    assert f'HOME={shlex.quote(str(expected_home))}' in start_cmd
    assert f'GEMINI_CLI_HOME={shlex.quote(str(expected_home))}' in start_cmd
    assert f'GEMINI_ROOT={shlex.quote(str(expected_root))}' in start_cmd


def test_prepare_gemini_home_overrides_keeps_cli_home_aligned_with_projected_state(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-runtime' / 'gemini'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg-cache'))

    env = prepare_gemini_home_overrides(runtime_dir, None)

    expected_home = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-state' / 'gemini' / 'home'
    expected_cache = tmp_path / 'xdg-cache' / 'ccb' / 'projects' / PathLayout(tmp_path).project_id[:16] / 'provider-cache' / 'gemini'
    assert env['HOME'] == str(expected_home)
    assert env['GEMINI_CLI_HOME'] == str(expected_home)
    assert env['GEMINI_ROOT'] == str(expected_home / '.gemini' / 'tmp')
    assert env['NPM_CONFIG_CACHE'] == str(expected_cache / 'npm')
    assert env['npm_config_cache'] == str(expected_cache / 'npm')
    assert env['XDG_CACHE_HOME'] == str(expected_cache / 'xdg')
    assert (expected_cache / 'npm').is_dir()
    assert (expected_cache / 'xdg').is_dir()
    assert (expected_home / '.gemini' / 'settings.json').is_file()
    assert not (expected_home / '.gemini' / '.gemini' / 'settings.json').exists()


def test_prepare_gemini_home_overrides_uses_runtime_local_cache_without_project_context(tmp_path) -> None:
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)

    env = prepare_gemini_home_overrides(runtime_dir, None, refresh_home=False)

    expected_cache = runtime_dir / 'rebuildable-cache' / 'gemini'
    assert env['NPM_CONFIG_CACHE'] == str(expected_cache / 'npm')
    assert env['npm_config_cache'] == str(expected_cache / 'npm')
    assert env['XDG_CACHE_HOME'] == str(expected_cache / 'xdg')
    assert (expected_cache / 'npm').is_dir()
    assert (expected_cache / 'xdg').is_dir()


def test_resolve_gemini_home_layout_rejects_non_managed_persisted_home(tmp_path) -> None:
    runtime_dir = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-runtime' / 'gemini'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    session_file = tmp_path / '.ccb' / '.gemini-agent1-session'
    legacy_home = tmp_path / 'legacy-global-home'
    session_file.write_text(
        json.dumps(
            {
                'gemini_home': str(legacy_home),
                'gemini_root': str(legacy_home / '.gemini' / 'tmp'),
            }
        )
        + '\n',
        encoding='utf-8',
    )

    layout = resolve_gemini_home_layout(runtime_dir, None)

    expected_home = tmp_path / '.ccb' / 'agents' / 'agent1' / 'provider-state' / 'gemini' / 'home'
    assert layout.home_root == expected_home
