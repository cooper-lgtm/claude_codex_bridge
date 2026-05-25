from __future__ import annotations

from pathlib import Path

from provider_backends.pane_log_support.lifecycle_common import attach_pane_log
from provider_backends.pane_log_support.lifecycle_recovery import respawn_existing_pane
from provider_backends.pane_log_support.session import now_str
from provider_core.registry import build_default_session_binding_map
from provider_core.session_binding_evidence_runtime.loading import binding_search_roots, load_provider_session
from terminal_runtime import TmuxBackend


RESTART_PANES_REASON = 'manual_restart_panes'


def build_project_restart_panes_handler(app):
    def handle(payload: dict) -> tuple[dict, object]:
        del payload
        agent_names = tuple(app.config.agents)

        def _after_response() -> None:
            try:
                with app.start_maintenance_lock:
                    restart_project_agent_panes_in_place(app, agent_names=agent_names)
            except Exception:
                # Keep ccbd alive; the supervision loop can repair failed panes later.
                return

        return {
            'status': 'scheduled',
            'agent_names': list(agent_names),
            'restart_mode': 'in_place',
            'recreate_reason': RESTART_PANES_REASON,
        }, _after_response

    return handle


def restart_project_agent_panes_in_place(app, *, agent_names: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    namespace = app.project_namespace.load()
    if namespace is None:
        raise RuntimeError('project namespace is not mounted')
    backend = TmuxBackend(socket_path=namespace.tmux_socket_path)
    results: list[dict[str, object]] = []
    for agent_name in agent_names:
        results.append(_restart_agent_pane(app, backend=backend, agent_name=str(agent_name)))
    return tuple(results)


def _restart_agent_pane(app, *, backend, agent_name: str) -> dict[str, object]:
    runtime = app.registry.get(agent_name)
    session = _load_agent_provider_session(app, agent_name=agent_name, runtime=runtime)
    pane_id = _restart_pane_id(runtime=runtime, session=session)
    if session is None:
        return {'agent': agent_name, 'status': 'skipped', 'reason': 'session_missing'}
    if not pane_id:
        return {'agent': agent_name, 'status': 'skipped', 'reason': 'pane_missing'}
    start_cmd = str(getattr(session, 'start_cmd', '') or '').strip()
    if not start_cmd:
        return {'agent': agent_name, 'status': 'skipped', 'reason': 'start_cmd_missing'}
    error = respawn_existing_pane(
        session,
        backend,
        pane_id,
        start_cmd=start_cmd,
        respawn=getattr(backend, 'respawn_pane', None),
        now_str_fn=now_str,
        attach_pane_log_fn=attach_pane_log,
    )
    if error is not None:
        return {'agent': agent_name, 'status': 'failed', 'reason': error, 'pane_id': pane_id}
    refreshed = app.runtime_service.refresh_provider_binding(agent_name, recover=True)
    return {
        'agent': agent_name,
        'status': 'restarted',
        'pane_id': str(getattr(refreshed, 'pane_id', None) or pane_id),
    }


def _restart_pane_id(*, runtime, session) -> str | None:
    for candidate in (
        getattr(runtime, 'pane_id', None),
        getattr(runtime, 'active_pane_id', None),
        getattr(session, 'pane_id', None),
    ):
        text = str(candidate or '').strip()
        if text.startswith('%'):
            return text
    return None


def _load_agent_provider_session(app, *, agent_name: str, runtime):
    spec = app.config.agents.get(agent_name)
    if spec is None:
        return None
    provider = str(getattr(spec, 'provider', '') or '').strip().lower()
    if not provider:
        return None
    adapter = _session_binding_adapter(app, provider)
    if adapter is None:
        return None
    workspace_path = _workspace_path(app, agent_name=agent_name, runtime=runtime)
    return load_provider_session(
        adapter=adapter,
        provider=provider,
        agent_name=agent_name,
        roots=binding_search_roots(workspace_path=workspace_path, project_root=Path(app.project_root)),
        ensure_usable=False,
        session_is_usable_fn=lambda _session: True,
    )


def _session_binding_adapter(app, provider: str):
    bindings = getattr(getattr(app, 'runtime_service', None), '_session_bindings', None)
    if not isinstance(bindings, dict):
        bindings = build_default_session_binding_map(include_optional=True)
    return bindings.get(provider)


def _workspace_path(app, *, agent_name: str, runtime) -> Path:
    text = str(getattr(runtime, 'workspace_path', '') or '').strip()
    if text:
        return Path(text)
    try:
        return Path(app.paths.workspace_path(agent_name))
    except Exception:
        return Path(app.project_root)


__all__ = [
    'RESTART_PANES_REASON',
    'build_project_restart_panes_handler',
    'restart_project_agent_panes_in_place',
]
