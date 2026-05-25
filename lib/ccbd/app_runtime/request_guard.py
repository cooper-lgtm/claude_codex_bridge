from __future__ import annotations

_STOPPING_GUARDED_OPS = frozenset({
    'submit',
    'attach',
    'start',
    'restore',
    'cancel',
    'ack',
    'resubmit',
    'retry',
    'project_restart_panes',
})


def rejection_for_request(app, op: str) -> str | None:
    if op not in _STOPPING_GUARDED_OPS:
        return None
    if lifecycle_is_stopping(_load_lifecycle(app)):
        return 'ccbd is unavailable: lifecycle_stopping'
    return None


def lifecycle_is_stopping(lifecycle) -> bool:
    if lifecycle is None:
        return False
    phase = str(getattr(lifecycle, 'phase', '') or '').strip()
    desired_state = str(getattr(lifecycle, 'desired_state', '') or '').strip()
    shutdown_intent = str(getattr(lifecycle, 'shutdown_intent', '') or '').strip()
    return phase == 'stopping' or (desired_state == 'stopped' and bool(shutdown_intent))


def _load_lifecycle(app):
    try:
        return app.lifecycle_store.load()
    except Exception:
        return None


__all__ = ['lifecycle_is_stopping', 'rejection_for_request']
