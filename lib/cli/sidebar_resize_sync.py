from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from terminal_runtime.env import isolated_tmux_env
from terminal_runtime.tmux import tmux_base


@dataclass(frozen=True)
class SidebarResizeSync:
    tmux_socket_path: Path
    session_name: str
    source_pane: str = ''
    source_window: str = ''
    project_id: str = ''
    from_stored_width: bool = False


@dataclass(frozen=True)
class _PaneRecord:
    session_name: str
    window_id: str
    window_name: str
    pane_id: str
    pane_width: int
    window_width: int
    project_id: str
    role: str
    sidebar_instance: str
    managed_by: str


RunFn = Callable[..., subprocess.CompletedProcess]


def maybe_handle_sidebar_resize_sync_command(tokens: list[str], *, stderr: TextIO) -> int | None:
    if not tokens or tokens[0] != '__sidebar-resize-sync':
        return None
    try:
        sync = _parse_sidebar_resize_sync(tokens[1:])
        sync_sidebar_resize(sync)
        return 0
    except Exception as exc:
        print(f'ccb sidebar resize sync failed: {exc}', file=stderr)
        return 1


def sync_sidebar_resize(sync: SidebarResizeSync, *, run_fn: RunFn | None = None) -> int | None:
    tmux_run = _tmux_runner(sync, run_fn=run_fn)
    panes = _list_panes(sync, tmux_run=tmux_run)
    source = next((pane for pane in panes if sync.source_pane and pane.pane_id == sync.source_pane), None)
    project_id = sync.project_id or (source.project_id if source is not None else '')
    source_sidebar = (
        _source_window_sidebar(panes, source=source, project_id=project_id)
        if source is not None
        else _source_window_sidebar_by_window(
            panes,
            session_name=sync.session_name,
            source_window=sync.source_window,
            project_id=project_id,
        )
    )
    if not project_id and source_sidebar is not None:
        project_id = source_sidebar.project_id

    if sync.from_stored_width:
        target_width = _session_sidebar_width(sync, tmux_run=tmux_run)
        if target_width <= 0 and source_sidebar is not None:
            target_width = source_sidebar.pane_width
    else:
        if source_sidebar is None:
            return None
        target_width = source_sidebar.pane_width
    if target_width <= 0:
        return None
    if not sync.from_stored_width:
        _set_session_sidebar_width(sync, tmux_run=tmux_run, width=target_width)
    resize_count = 0
    _set_session_sync_guard(sync, tmux_run=tmux_run, enabled=True)
    try:
        for pane in panes:
            if pane.session_name != sync.session_name:
                continue
            if pane.role != 'sidebar' or pane.managed_by != 'ccbd':
                continue
            if project_id and pane.project_id != project_id:
                continue
            clamped_width = _clamp_sidebar_width(target_width, pane.window_width)
            if clamped_width <= 0 or pane.pane_width == clamped_width:
                continue
            tmux_run(['resize-pane', '-t', pane.pane_id, '-x', str(clamped_width)])
            resize_count += 1
    finally:
        _set_session_sync_guard(sync, tmux_run=tmux_run, enabled=False)
    return resize_count


def _source_window_sidebar(
    panes: list[_PaneRecord],
    *,
    source: _PaneRecord,
    project_id: str,
) -> _PaneRecord | None:
    sidebars = [
        pane
        for pane in panes
        if pane.session_name == source.session_name
        and pane.role == 'sidebar'
        and pane.managed_by == 'ccbd'
        and (not project_id or pane.project_id == project_id)
    ]
    for pane in sidebars:
        if pane.window_id and pane.window_id == source.window_id:
            return pane
    for pane in sidebars:
        if pane.sidebar_instance and pane.sidebar_instance == source.window_name:
            return pane
    return source if source.role == 'sidebar' and source.managed_by == 'ccbd' else None


def _source_window_sidebar_by_window(
    panes: list[_PaneRecord],
    *,
    session_name: str,
    source_window: str,
    project_id: str,
) -> _PaneRecord | None:
    token = str(source_window or '').strip()
    if not token:
        return None
    sidebars = [
        pane
        for pane in panes
        if pane.session_name == session_name
        and pane.role == 'sidebar'
        and pane.managed_by == 'ccbd'
        and (not project_id or pane.project_id == project_id)
    ]
    for pane in sidebars:
        if pane.window_id and pane.window_id == token:
            return pane
    for pane in sidebars:
        if pane.window_name and pane.window_name == token:
            return pane
    for pane in sidebars:
        if pane.sidebar_instance and pane.sidebar_instance == token:
            return pane
    return None


def _list_panes(sync: SidebarResizeSync, *, tmux_run: Callable[[list[str]], subprocess.CompletedProcess]) -> list[_PaneRecord]:
    fmt = '\t'.join(
        [
            '#{session_name}',
            '#{window_id}',
            '#{window_name}',
            '#{pane_id}',
            '#{pane_width}',
            '#{window_width}',
            '#{@ccb_project_id}',
            '#{@ccb_role}',
            '#{@ccb_sidebar_instance}',
            '#{@ccb_managed_by}',
        ]
    )
    cp = tmux_run(['list-panes', '-a', '-F', fmt])
    if int(getattr(cp, 'returncode', 1) or 0) != 0:
        return []
    records: list[_PaneRecord] = []
    for line in (getattr(cp, 'stdout', '') or '').splitlines():
        parts = [part.strip() for part in line.split('\t')]
        if len(parts) != 10:
            continue
        session_name, window_id, window_name, pane_id, pane_width, window_width, project_id, role, sidebar_instance, managed_by = parts
        if session_name != sync.session_name or not pane_id.startswith('%'):
            continue
        records.append(
            _PaneRecord(
                session_name=session_name,
                window_id=window_id,
                window_name=window_name,
                pane_id=pane_id,
                pane_width=_positive_int(pane_width),
                window_width=_positive_int(window_width),
                project_id=project_id,
                role=role,
                sidebar_instance=sidebar_instance,
                managed_by=managed_by,
            )
        )
    return records


def _session_sidebar_width(
    sync: SidebarResizeSync,
    *,
    tmux_run: Callable[[list[str]], subprocess.CompletedProcess],
) -> int:
    cp = tmux_run(['show-option', '-qv', '-t', sync.session_name, '@ccb_sidebar_width_cells'])
    if int(getattr(cp, 'returncode', 1) or 0) != 0:
        return 0
    return _positive_int(((getattr(cp, 'stdout', '') or '').splitlines() or [''])[0])


def _set_session_sidebar_width(
    sync: SidebarResizeSync,
    *,
    tmux_run: Callable[[list[str]], subprocess.CompletedProcess],
    width: int,
) -> None:
    tmux_run(
        [
            'set-option',
            '-t',
            sync.session_name,
            '@ccb_sidebar_width_cells',
            str(max(1, int(width))),
        ]
    )


def _set_session_sync_guard(
    sync: SidebarResizeSync,
    *,
    tmux_run: Callable[[list[str]], subprocess.CompletedProcess],
    enabled: bool,
) -> None:
    if enabled:
        tmux_run(
            [
                'set-option',
                '-t',
                sync.session_name,
                '@ccb_sidebar_sync_guard',
                '1',
            ]
        )
        return
    tmux_run(['set-option', '-u', '-t', sync.session_name, '@ccb_sidebar_sync_guard'])


def _clamp_sidebar_width(width: int, window_width: int) -> int:
    if window_width <= 0:
        return max(1, int(width))
    min_user_width = 10 if window_width > 20 else 1
    return max(1, min(max(1, window_width - min_user_width), int(width)))


def _tmux_runner(sync: SidebarResizeSync, *, run_fn: RunFn | None) -> Callable[[list[str]], subprocess.CompletedProcess]:
    runner = run_fn or subprocess.run
    base = tmux_base(socket_path=str(sync.tmux_socket_path))

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return runner(
            [*base, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=isolated_tmux_env(),
        )

    return run


def _parse_sidebar_resize_sync(argv: list[str]) -> SidebarResizeSync:
    parser = argparse.ArgumentParser(prog='ccb __sidebar-resize-sync', add_help=False)
    parser.add_argument('--tmux-socket', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--source-pane', default='')
    parser.add_argument('--source-window', default='')
    parser.add_argument('--project-id', default='')
    parser.add_argument('--from-stored-width', action='store_true')
    args = parser.parse_args(argv)
    return SidebarResizeSync(
        tmux_socket_path=Path(args.tmux_socket),
        session_name=str(args.session or '').strip(),
        source_pane=str(args.source_pane or '').strip(),
        source_window=str(args.source_window or '').strip(),
        project_id=str(args.project_id or '').strip(),
        from_stored_width=bool(args.from_stored_width),
    )


def _positive_int(value: object) -> int:
    try:
        parsed = int(str(value or '').strip())
    except Exception:
        return 0
    return max(0, parsed)


__all__ = [
    'SidebarResizeSync',
    'maybe_handle_sidebar_resize_sync_command',
    'sync_sidebar_resize',
]
