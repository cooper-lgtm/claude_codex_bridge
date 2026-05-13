from __future__ import annotations

import time


def request_remote_stop(
    context,
    *,
    force: bool,
    connect_mounted_daemon_fn,
    record_shutdown_intent_fn,
    ccbd_client_cls,
    summary_from_stop_all_payload_fn,
    stop_all_timeout_s: float,
    service_error_cls,
):
    try:
        handle = connect_mounted_daemon_fn(context, allow_restart_stale=False)
    except service_error_cls:
        return None
    if handle is None or handle.client is None:
        return None
    try:
        record_shutdown_intent_fn(context, reason='kill')
        stop_all_client = (
            ccbd_client_cls(context.paths.ccbd_socket_path, timeout_s=stop_all_timeout_s)
            if isinstance(handle.client, ccbd_client_cls)
            else handle.client
        )
        payload = stop_all_client.stop_all(force=force)
    except Exception:
        if not force:
            raise
        return None
    return summary_from_stop_all_payload_fn(payload)


def resolve_shutdown_summary(
    context,
    *,
    remote_summary,
    force: bool,
    shutdown_daemon_fn,
    await_remote_shutdown_fn,
    service_error_cls,
    kill_summary_cls,
):
    if remote_summary is not None:
        return await_remote_shutdown_fn(context, force=force)
    try:
        return shutdown_daemon_fn(context, force=force)
    except service_error_cls:
        if not force:
            raise
        return kill_summary_cls(
            project_id=context.project.project_id,
            state='unmounted',
            socket_path=str(context.paths.ccbd_socket_path),
            forced=force,
        )


def await_remote_shutdown(
    context,
    *,
    force: bool,
    inspect_daemon_fn,
    lease_health_cls,
    kill_summary_cls,
    timeout_s: float = 2.5,
    expected_pids: tuple[int, ...] = (),
    lease_pid_fn=None,
    keeper_pid_fn=None,
    wait_for_pid_exit_fn=None,
    wait_for_keeper_exit_fn=None,
    is_pid_alive_fn=None,
    terminate_pid_tree_fn=None,
    shutdown_timeout_s: float = 1.0,
):
    deadline = time.time() + max(0.1, float(timeout_s))
    last_inspection = None
    expected_pid_set = _clean_pid_set(expected_pids)
    daemon_pid = 0
    keeper_pid = 0
    while time.time() < deadline:
        _, _, inspection = inspect_daemon_fn(context)
        last_inspection = inspection
        lease = getattr(inspection, 'lease', None)
        if expected_pid_set:
            tracked_exited = _tracked_pid_set_exited(expected_pid_set, is_pid_alive_fn=is_pid_alive_fn)
        else:
            daemon_pid = _remember_daemon_pid(daemon_pid, lease, lease_pid_fn=lease_pid_fn)
            keeper_pid = _remember_keeper_pid(context, keeper_pid, lease, keeper_pid_fn=keeper_pid_fn)
            tracked_exited = _tracked_pids_exited(
                daemon_pid,
                keeper_pid,
                is_pid_alive_fn=is_pid_alive_fn,
            )
        if _remote_shutdown_observed(inspection, lease_health_cls=lease_health_cls) and tracked_exited:
            break
        time.sleep(0.05)
    if expected_pid_set:
        for pid in sorted(expected_pid_set):
            _terminate_lingering_pid(
                pid,
                wait_for_pid_exit_fn=wait_for_pid_exit_fn,
                is_pid_alive_fn=is_pid_alive_fn,
                terminate_pid_tree_fn=terminate_pid_tree_fn,
                timeout_s=shutdown_timeout_s,
            )
    else:
        _terminate_lingering_pid(
            daemon_pid,
            wait_for_pid_exit_fn=wait_for_pid_exit_fn,
            is_pid_alive_fn=is_pid_alive_fn,
            terminate_pid_tree_fn=terminate_pid_tree_fn,
            timeout_s=shutdown_timeout_s,
        )
        if keeper_pid != daemon_pid:
            _terminate_lingering_keeper(
                context,
                keeper_pid,
                wait_for_keeper_exit_fn=wait_for_keeper_exit_fn,
                is_pid_alive_fn=is_pid_alive_fn,
                terminate_pid_tree_fn=terminate_pid_tree_fn,
                timeout_s=shutdown_timeout_s,
            )
    try:
        _, _, last_inspection = inspect_daemon_fn(context)
    except Exception:
        pass
    return kill_summary_cls(
        project_id=context.project.project_id,
        state='unmounted' if last_inspection is None else _inspection_phase(last_inspection),
        socket_path=str(context.paths.ccbd_socket_path),
        forced=force,
    )


def _inspection_phase(inspection) -> str:
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    if phase:
        return phase
    lease = getattr(inspection, 'lease', None)
    mount_state = str(getattr(getattr(lease, 'mount_state', None), 'value', '') or '').strip()
    return mount_state or 'unmounted'


def _remote_shutdown_observed(inspection, *, lease_health_cls) -> bool:
    if _inspection_phase(inspection) == 'unmounted':
        return True
    return bool(
        not getattr(inspection, 'socket_connectable', False)
        and getattr(inspection, 'health', None)
        in {
            lease_health_cls.MISSING,
            lease_health_cls.UNMOUNTED,
            lease_health_cls.STALE,
        }
    )


def _remember_daemon_pid(current: int, lease, *, lease_pid_fn) -> int:
    if current > 0 or lease_pid_fn is None:
        return current
    try:
        pid = int(lease_pid_fn(lease) or 0)
    except Exception:
        return current
    return pid if pid > 0 else current


def _remember_keeper_pid(context, current: int, lease, *, keeper_pid_fn) -> int:
    if current > 0 or keeper_pid_fn is None:
        return current
    try:
        pid = int(keeper_pid_fn(context, lease) or 0)
    except Exception:
        return current
    return pid if pid > 0 else current


def _tracked_pids_exited(daemon_pid: int, keeper_pid: int, *, is_pid_alive_fn) -> bool:
    return _tracked_pid_set_exited({int(daemon_pid or 0), int(keeper_pid or 0)}, is_pid_alive_fn=is_pid_alive_fn)


def _tracked_pid_set_exited(pids: set[int], *, is_pid_alive_fn) -> bool:
    if is_pid_alive_fn is None:
        return True
    for pid in pids:
        if pid > 0 and is_pid_alive_fn(pid):
            return False
    return True


def _clean_pid_set(pids: tuple[int, ...]) -> set[int]:
    cleaned: set[int] = set()
    for pid in pids:
        try:
            value = int(pid)
        except Exception:
            continue
        if value > 0:
            cleaned.add(value)
    return cleaned


def _terminate_lingering_pid(
    pid: int,
    *,
    wait_for_pid_exit_fn,
    is_pid_alive_fn,
    terminate_pid_tree_fn,
    timeout_s: float,
) -> None:
    if pid <= 0 or is_pid_alive_fn is None or terminate_pid_tree_fn is None:
        return
    if not is_pid_alive_fn(pid):
        return
    if wait_for_pid_exit_fn is not None and wait_for_pid_exit_fn(pid, timeout_s=timeout_s):
        return
    terminate_pid_tree_fn(pid, timeout_s=timeout_s, is_pid_alive_fn=is_pid_alive_fn)


def _terminate_lingering_keeper(
    context,
    pid: int,
    *,
    wait_for_keeper_exit_fn,
    is_pid_alive_fn,
    terminate_pid_tree_fn,
    timeout_s: float,
) -> None:
    if pid <= 0 or is_pid_alive_fn is None or terminate_pid_tree_fn is None:
        return
    if not is_pid_alive_fn(pid):
        return
    if wait_for_keeper_exit_fn is not None and wait_for_keeper_exit_fn(context, timeout_s=timeout_s):
        return
    terminate_pid_tree_fn(pid, timeout_s=timeout_s, is_pid_alive_fn=is_pid_alive_fn)


__all__ = ['await_remote_shutdown', 'request_remote_stop', 'resolve_shutdown_summary']
