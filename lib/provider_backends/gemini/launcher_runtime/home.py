from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from provider_core.source_home import current_provider_source_home
from provider_profiles import provider_api_env_keys
from project_memory import (
    ensure_project_memory,
    load_memory_sources,
    read_memory_source,
    render_memory_bundle,
)
from project_memory.hashing import sha256_text
from storage.atomic import atomic_write_text
from storage.paths import PathLayout

from ..home_layout import GeminiHomeLayout, gemini_layout_for_home, gemini_layout_from_session_data
from .session_paths import read_session_payload, session_file_for_runtime_dir, state_dir_for_runtime_dir

_GEMINI_LOGIN_AUTH_FILENAMES = ('oauth_creds.json', 'google_accounts.json')


def resolve_gemini_home_layout(runtime_dir: Path, profile) -> GeminiHomeLayout:
    explicit_runtime_home = _profile_runtime_home(profile)
    if explicit_runtime_home is not None:
        return gemini_layout_for_home(explicit_runtime_home)

    managed_home = _managed_isolated_home(runtime_dir)
    existing = _existing_layout(runtime_dir, managed_home=managed_home)
    if existing is not None:
        return existing

    return gemini_layout_for_home(managed_home)


def prepare_gemini_home_overrides(
    runtime_dir: Path,
    profile,
    *,
    refresh_home: bool = True,
    project_root: Path | None = None,
    agent_name: str | None = None,
    workspace_path: Path | None = None,
    memory_projection_event_path: Path | None = None,
    memory_projection_marker_path: Path | None = None,
) -> dict[str, str]:
    layout = resolve_gemini_home_layout(runtime_dir, profile)
    if refresh_home:
        materialize_gemini_home_config(
            layout.home_root,
            profile=profile,
            project_root=project_root,
            agent_name=agent_name,
            workspace_path=workspace_path,
            memory_projection_event_path=memory_projection_event_path,
            memory_projection_marker_path=memory_projection_marker_path,
        )
    cache_root = _gemini_shared_cache_root(project_root, runtime_dir)
    return {
        'HOME': str(layout.home_root),
        'GEMINI_CLI_HOME': str(layout.home_root),
        'GEMINI_ROOT': str(layout.tmp_root),
        'NPM_CONFIG_CACHE': str(cache_root / 'npm'),
        'npm_config_cache': str(cache_root / 'npm'),
        'XDG_CACHE_HOME': str(cache_root / 'xdg'),
    }


def _profile_runtime_home(profile) -> Path | None:
    del profile
    return None


def _existing_layout(runtime_dir: Path, *, managed_home: Path) -> GeminiHomeLayout | None:
    session_file = session_file_for_runtime_dir(runtime_dir)
    if session_file is None or not session_file.is_file():
        return None
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return None
    layout = gemini_layout_from_session_data(data)
    if layout is None:
        return None
    return layout if _is_within_home_root(layout.home_root, managed_home) else None


def _managed_isolated_home(runtime_dir: Path) -> Path:
    state_dir = state_dir_for_runtime_dir(runtime_dir)
    if state_dir is not None:
        return state_dir / 'home'
    return Path(runtime_dir).expanduser() / 'gemini-home'


def _gemini_shared_cache_root(project_root: Path | None, runtime_dir: Path) -> Path:
    cache_root = _external_project_cache_root(project_root, runtime_dir)
    if cache_root is None:
        cache_root = Path(runtime_dir).expanduser() / 'rebuildable-cache'
    root = cache_root / 'gemini'
    (root / 'npm').mkdir(parents=True, exist_ok=True)
    (root / 'xdg').mkdir(parents=True, exist_ok=True)
    return root


def _external_project_cache_root(project_root: Path | None, runtime_dir: Path) -> Path | None:
    root = Path(project_root).expanduser() if project_root is not None else _project_root_from_runtime_dir(runtime_dir)
    if root is None:
        return None
    try:
        layout = PathLayout(root)
    except Exception:
        return None
    return _user_cache_home() / 'ccb' / 'projects' / layout.project_id[:16] / 'provider-cache'


def _user_cache_home() -> Path:
    raw = str(os.environ.get('XDG_CACHE_HOME') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.cache'


def _project_root_from_runtime_dir(runtime_dir: Path) -> Path | None:
    ccb_dir = _project_ccb_dir(runtime_dir)
    if ccb_dir is None:
        return None
    return ccb_dir.parent


def _project_ccb_dir(runtime_dir: Path) -> Path | None:
    current = Path(runtime_dir).expanduser()
    for candidate in (current, *current.parents):
        if candidate.name == '.ccb':
            return candidate
    return None


def _is_within_home_root(candidate: Path, managed_home: Path) -> bool:
    normalized_candidate = _normalize_path(candidate)
    normalized_managed = _normalize_path(managed_home)
    if normalized_candidate is None or normalized_managed is None:
        return False
    try:
        normalized_candidate.relative_to(normalized_managed)
        return True
    except Exception:
        return False


def _normalize_path(value: object) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        try:
            return Path(value).expanduser()
        except Exception:
            return None


def _prepare_managed_home(layout: GeminiHomeLayout) -> None:
    layout.home_root.mkdir(parents=True, exist_ok=True)
    layout.gemini_dir.mkdir(parents=True, exist_ok=True)
    layout.tmp_root.mkdir(parents=True, exist_ok=True)
    _ensure_json_file(layout.settings_path)
    _ensure_json_file(layout.trusted_folders_path)


def _ensure_json_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}\n', encoding='utf-8')


def materialize_gemini_home_config(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
    project_root: Path | None = None,
    agent_name: str | None = None,
    workspace_path: Path | None = None,
    memory_projection_event_path: Path | None = None,
    memory_projection_marker_path: Path | None = None,
) -> GeminiHomeLayout:
    layout = gemini_layout_for_home(target_home)
    _prepare_managed_home(layout)
    source_root = Path(source_home).expanduser() if source_home is not None else _system_home_root()
    memory_result = _memory_projection_result(
        status='skipped',
        reason='source_home_is_target_home',
        path=layout.gemini_dir / 'GEMINI.md',
    )
    if layout.home_root != source_root:
        _materialize_settings(source_root, layout, profile=profile)
        _materialize_env_file(source_root, layout, profile=profile)
        _materialize_trusted_folders(source_root, layout)
        _materialize_auth(source_root, layout, profile=profile)
        memory_result = _materialize_gemini_memory(
            source_root,
            layout,
            profile=profile,
            project_root=project_root,
            agent_name=agent_name,
            workspace_path=workspace_path,
        )
    _record_memory_projection_event(
        memory_result,
        event_path=memory_projection_event_path,
        marker_path=memory_projection_marker_path,
        agent_name=agent_name,
    )
    return layout


def _materialize_settings(source_home: Path, layout: GeminiHomeLayout, *, profile) -> None:
    projected = _projected_settings_payload(source_home / '.gemini' / 'settings.json', profile=profile)
    existing = _read_json_object(layout.settings_path)
    merged = _merge_settings_payload(projected, existing=existing)
    if merged is None:
        return
    _write_json_object(layout.settings_path, merged)


def _materialize_trusted_folders(source_home: Path, layout: GeminiHomeLayout) -> None:
    projected = _read_json_object(source_home / '.gemini' / 'trustedFolders.json')
    existing = _read_json_object(layout.trusted_folders_path)
    merged = _merge_object_payload(projected, existing=existing)
    if merged is None:
        return
    _write_json_object(layout.trusted_folders_path, merged)


def _materialize_env_file(source_home: Path, layout: GeminiHomeLayout, *, profile) -> None:
    target_env = layout.gemini_dir / '.env'
    env_payload = _projected_dotenv_payload(source_home / '.gemini' / '.env', profile=profile)
    if not env_payload:
        _remove_file(target_env)
        return
    _write_env_file(target_env, env_payload)


def _materialize_auth(source_home: Path, layout: GeminiHomeLayout, *, profile) -> None:
    if not _should_project_login_auth(source_home / '.gemini' / 'settings.json', profile=profile):
        for filename in _GEMINI_LOGIN_AUTH_FILENAMES:
            _remove_file(layout.gemini_dir / filename)
        return
    for filename in _GEMINI_LOGIN_AUTH_FILENAMES:
        _sync_file(source_home / '.gemini' / filename, layout.gemini_dir / filename)


def _projected_settings_payload(source_settings_path: Path, *, profile) -> dict[str, object] | None:
    source_payload = _read_json_object(source_settings_path)
    if not source_payload:
        return {} if _needs_settings_stub(profile) else None

    source_env = dict(source_payload.get('env') or {}) if isinstance(source_payload.get('env'), dict) else {}
    env_payload = dict(source_env) if _inherits_config(profile) else {}
    if _inherits_api(profile):
        for key in provider_api_env_keys('gemini'):
            value = source_env.get(key)
            if value is not None:
                env_payload[key] = value
    else:
        for key in provider_api_env_keys('gemini'):
            env_payload.pop(key, None)

    payload: dict[str, object] = dict(source_payload) if _inherits_config(profile) else {}
    projected_selected_type = _projected_auth_selected_type(_selected_auth_type(source_payload), profile=profile)
    if projected_selected_type is not None:
        _set_selected_auth_type(payload, projected_selected_type)
    else:
        _clear_selected_auth_type(payload)
    if env_payload:
        payload['env'] = env_payload
    else:
        payload.pop('env', None)
    if payload:
        return payload
    return {} if _needs_settings_stub(profile) else None


def _merge_settings_payload(
    projected: dict[str, object] | None,
    *,
    existing: dict[str, object] | None,
) -> dict[str, object] | None:
    projected_payload = dict(projected or {})
    existing_payload = dict(existing or {})
    merged = dict(projected_payload)
    hooks = existing_payload.get('hooks')
    if hooks is not None:
        merged['hooks'] = hooks
    context_file_name = existing_payload.get('contextFileName')
    if context_file_name is not None:
        merged['contextFileName'] = context_file_name
    if merged:
        return merged
    if existing_payload:
        return existing_payload
    return None


def _merge_object_payload(
    projected: dict[str, object] | None,
    *,
    existing: dict[str, object] | None,
) -> dict[str, object] | None:
    merged = dict(projected or {})
    merged.update(dict(existing or {}))
    return merged if merged else None


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_object(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _projected_dotenv_payload(source_env_path: Path, *, profile) -> dict[str, str]:
    if not _inherits_api(profile):
        return {}
    source_payload = _read_env_file(source_env_path)
    if not source_payload:
        return {}
    allowed = provider_api_env_keys('gemini')
    return {
        key: value
        for key, value in source_payload.items()
        if key in allowed and str(value).strip()
    }


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return {}
    payload: dict[str, str] = {}
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        payload[key] = value
    return payload


def _parse_env_line(line: str) -> tuple[str, str] | None:
    raw = str(line or '').strip()
    if not raw or raw.startswith('#'):
        return None
    if raw.startswith('export '):
        raw = raw[len('export ') :].lstrip()
    if '=' not in raw:
        return None
    key, value = raw.split('=', 1)
    key = key.strip()
    if not _is_env_key(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _is_env_key(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first == '_' or first.isalpha()):
        return False
    return all(ch == '_' or ch.isalnum() for ch in value)


def _write_env_file(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{key}={_quote_env_value(value)}' for key, value in sorted(payload.items())]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _quote_env_value(value: object) -> str:
    raw = str(value)
    escaped = raw.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'"{escaped}"'


def _needs_settings_stub(profile) -> bool:
    return bool(_inherits_api(profile) or _inherits_auth(profile) or _inherits_config(profile))


def _inherits_api(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_api', True))


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


def _inherits_memory(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_memory', True))


def _materialize_gemini_memory(
    source_home: Path,
    layout: GeminiHomeLayout,
    *,
    profile,
    project_root: Path | None,
    agent_name: str | None,
    workspace_path: Path | None,
) -> dict[str, object]:
    target = layout.gemini_dir / 'GEMINI.md'
    if not _inherits_memory(profile):
        _remove_file(target)
        _clear_context_file_name(layout)
        return _memory_projection_result(
            status='skipped',
            reason='inherit_memory_disabled',
            path=target,
        )
    if project_root is None or agent_name is None:
        return _memory_projection_result(
            status='failed',
            reason='missing_project_context',
            path=target,
        )
    root = Path(project_root).expanduser()
    try:
        warnings: list[str] = []
        ensure_result = ensure_project_memory(root)
        if ensure_result.warning:
            warnings.append(ensure_result.warning)
        extra_sources = tuple(
            source
            for source in (
                read_memory_source(
                    kind='provider_user_memory',
                    title='Provider User Memory',
                    path=source_home / '.gemini' / 'GEMINI.md',
                    include_missing=False,
                ),
            )
            if source is not None
        )
        sources = load_memory_sources(
            root,
            agent_name=agent_name,
            provider='gemini',
            extra_sources=extra_sources,
        )
        warnings.extend(source.warning for source in sources if source.warning)
        rendered = render_memory_bundle(
            project_root=root,
            agent_name=agent_name,
            provider='gemini',
            sources=sources,
            workspace_path=workspace_path,
        )
        digest = sha256_text(rendered)
        unchanged = _text_file_sha256(target) == digest
        if not unchanged:
            atomic_write_text(target, rendered)
        _ensure_context_file_name(layout)
        return _memory_projection_result(
            status='skipped' if unchanged else 'ok',
            reason='unchanged' if unchanged else 'written',
            path=target,
            sha256=digest,
            source_count=len(sources),
            warnings=warnings,
        )
    except Exception as exc:
        return _memory_projection_result(
            status='failed',
            reason=type(exc).__name__,
            path=target,
            error_detail=str(exc),
        )


def _memory_projection_result(
    *,
    status: str,
    reason: str,
    path: Path,
    sha256: str = '',
    source_count: int = 0,
    warnings: list[str] | tuple[str, ...] = (),
    error_detail: str = '',
) -> dict[str, object]:
    return {
        'status': status,
        'reason': reason,
        'path': str(path),
        'sha256': sha256,
        'source_count': source_count,
        'warnings': tuple(str(item) for item in warnings if str(item)),
        'error_detail': str(error_detail or ''),
    }


def _record_memory_projection_event(
    result: dict[str, object],
    *,
    event_path: Path | None,
    marker_path: Path | None,
    agent_name: str | None,
) -> None:
    if event_path is None or marker_path is None or not agent_name:
        return
    status = str(result.get('status') or 'unknown')
    reason = str(result.get('reason') or '')
    signature = {
        'status': status,
        'reason': reason,
        'path': str(result.get('path') or ''),
        'sha256': str(result.get('sha256') or ''),
        'warnings': list(result.get('warnings') or ()),
    }
    marker = Path(marker_path)
    if _same_memory_projection_signature(marker, signature):
        return
    event = {
        'record_type': 'agent_event',
        'event_type': f'gemini_memory_projection_{status}',
        'provider': 'gemini',
        'agent_name': agent_name,
        'status': status,
        'reason': reason,
        'projection_path': signature['path'],
        'sha256': signature['sha256'],
        'source_count': int(result.get('source_count') or 0),
        'warnings': signature['warnings'],
        'error_detail': str(result.get('error_detail') or ''),
        'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    try:
        target = Path(event_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(signature, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        return


def _same_memory_projection_signature(path: Path, payload: dict[str, object]) -> bool:
    try:
        existing = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    if existing == payload:
        return True
    if payload.get('status') == 'skipped' and payload.get('reason') == 'unchanged':
        return (
            bool(payload.get('sha256'))
            and existing.get('path') == payload.get('path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('warnings') == payload.get('warnings')
        )
    if payload.get('status') == 'skipped':
        return (
            existing.get('reason') == payload.get('reason')
            and existing.get('path') == payload.get('path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('warnings') == payload.get('warnings')
        )
    return False


def _ensure_context_file_name(layout: GeminiHomeLayout) -> None:
    payload = _read_json_object(layout.settings_path) or {}
    if payload.get('contextFileName') == 'GEMINI.md':
        return
    payload['contextFileName'] = 'GEMINI.md'
    _write_json_object(layout.settings_path, payload)


def _clear_context_file_name(layout: GeminiHomeLayout) -> None:
    payload = _read_json_object(layout.settings_path) or {}
    if payload.get('contextFileName') != 'GEMINI.md':
        return
    payload.pop('contextFileName', None)
    _write_json_object(layout.settings_path, payload)


def _text_file_sha256(path: Path) -> str:
    try:
        return sha256_text(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return ''


def _should_project_login_auth(source_settings_path: Path, *, profile) -> bool:
    if not _inherits_auth(profile):
        return False
    selected_type = _selected_auth_type(_read_json_object(source_settings_path))
    return selected_type in {'oauth-personal'}


def _selected_auth_type(payload: dict[str, object] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    security = payload.get('security')
    if not isinstance(security, dict):
        return None
    auth = security.get('auth')
    if not isinstance(auth, dict):
        return None
    raw = str(auth.get('selectedType') or '').strip()
    return raw or None


def _projected_auth_selected_type(selected_type: str | None, *, profile) -> str | None:
    normalized = str(selected_type or '').strip()
    if not normalized:
        return None
    if normalized in {'oauth-personal', 'compute-default-credentials'}:
        return normalized if _inherits_auth(profile) else None
    if normalized in {'gemini-api-key', 'vertex-ai'}:
        return normalized if _inherits_api(profile) else None
    return normalized if (_inherits_api(profile) or _inherits_auth(profile)) else None


def _set_selected_auth_type(payload: dict[str, object], selected_type: str) -> None:
    security = payload.get('security')
    if not isinstance(security, dict):
        security = {}
    auth = security.get('auth')
    if not isinstance(auth, dict):
        auth = {}
    auth['selectedType'] = selected_type
    security['auth'] = auth
    payload['security'] = security


def _clear_selected_auth_type(payload: dict[str, object]) -> None:
    security = payload.get('security')
    if not isinstance(security, dict):
        return
    auth = security.get('auth')
    if isinstance(auth, dict):
        auth.pop('selectedType', None)
        if auth:
            security['auth'] = auth
        else:
            security.pop('auth', None)
    if security:
        payload['security'] = security
    else:
        payload.pop('security', None)


def _sync_file(source: Path, target: Path) -> None:
    if not source.is_file():
        _remove_file(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except Exception:
        pass


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _system_home_root() -> Path:
    return current_provider_source_home()


__all__ = [
    'materialize_gemini_home_config',
    'prepare_gemini_home_overrides',
    'resolve_gemini_home_layout',
]
