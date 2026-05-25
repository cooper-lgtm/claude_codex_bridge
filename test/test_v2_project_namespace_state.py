from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from ccbd.services.project_namespace import ProjectNamespaceController
from ccbd.services.project_namespace_runtime import build_namespace_topology_plan
from ccbd.services.project_namespace_runtime.backend import prepare_server
from ccbd.services.project_namespace_state import (
    ProjectNamespaceEvent,
    ProjectNamespaceEventStore,
    ProjectNamespaceState,
    ProjectNamespaceStateStore,
)
from storage.paths import PathLayout
from agents.config_loader import load_project_config


def test_project_namespace_state_store_round_trip(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo')
    state = ProjectNamespaceState(
        project_id='proj-1',
        namespace_epoch=3,
        tmux_socket_path=str(layout.ccbd_tmux_socket_path),
        tmux_session_name=layout.ccbd_tmux_session_name,
        layout_version=3,
        layout_signature='cmd; agent1:codex',
        control_window_name=layout.ccbd_tmux_control_window_name,
        control_window_id='@1',
        workspace_window_name=layout.ccbd_tmux_workspace_window_name,
        workspace_window_id='@2',
        workspace_epoch=4,
        ui_attachable=True,
        last_started_at='2026-04-03T01:00:00Z',
        last_destroyed_at='2026-04-03T00:55:00Z',
        last_destroy_reason='kill',
    )

    store = ProjectNamespaceStateStore(layout)
    store.save(state)
    loaded = store.load()

    assert loaded == state
    assert loaded is not None
    assert loaded.summary_fields()['namespace_tmux_socket_path'] == str(layout.ccbd_tmux_socket_path)


def test_path_layout_normalizes_tmux_session_name_for_tmux_targets(tmp_path: Path) -> None:
    layout = PathLayout(tmp_path / 'repo.with.dots')

    assert layout.ccbd_tmux_session_name.startswith('ccb-')
    assert '.' not in layout.ccbd_tmux_session_name


@dataclass
class _FakeTmuxBackend:
    socket_path: str | None = None
    sessions: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    active_windows: dict[str, str] = field(default_factory=dict)
    pane_titles: dict[str, str] = field(default_factory=dict)
    pane_options: dict[str, dict[str, str]] = field(default_factory=dict)
    pane_widths: dict[str, int] = field(default_factory=dict)
    session_options: dict[str, dict[str, str]] = field(default_factory=dict)
    window_options: dict[str, dict[str, str]] = field(default_factory=dict)
    hooks: dict[str, dict[str, str]] = field(default_factory=dict)
    split_calls: list[tuple[str, str, int]] = field(default_factory=list)
    resize_calls: list[tuple[str, int]] = field(default_factory=list)
    tmux_calls: list[tuple[list[str], bool]] = field(default_factory=list)
    window_visibility_lag: dict[str, int] = field(default_factory=dict)
    pane_visibility_lag: dict[str, int] = field(default_factory=dict)
    pane_counter: int = 0
    window_counter: int = 0
    server_killed: bool = False

    def _alloc_pane(self) -> str:
        self.pane_counter += 1
        return f'%{self.pane_counter}'

    def _alloc_window(self) -> str:
        self.window_counter += 1
        return f'@{self.window_counter}'

    def _session_windows(self, session_name: str) -> list[dict[str, object]]:
        return self.sessions.setdefault(session_name, [])

    def _create_window(self, session_name: str, window_name: str) -> dict[str, object]:
        pane_id = self._alloc_pane()
        record = {
            'id': self._alloc_window(),
            'name': window_name,
            'width': 160,
            'panes': [pane_id],
        }
        self.pane_widths[pane_id] = int(record['width'])
        self._session_windows(session_name).append(record)
        self.active_windows.setdefault(session_name, window_name)
        return record

    def split_pane(
        self,
        parent_pane_id: str,
        direction: str,
        percent: int,
        cmd: str | None = None,
        cwd: str | None = None,
    ) -> str:
        del cmd, cwd
        self.split_calls.append((parent_pane_id, direction, percent))
        for windows in self.sessions.values():
            for record in windows:
                panes = record['panes']
                if parent_pane_id in panes:
                    parent_width = int(self.pane_widths.get(parent_pane_id, record.get('width', 160)) or 160)
                    pane_id = self._alloc_pane()
                    panes.append(pane_id)
                    if direction == 'right':
                        new_width = max(1, min(parent_width - 1, round(parent_width * (percent / 100.0))))
                        self.pane_widths[pane_id] = new_width
                        self.pane_widths[parent_pane_id] = max(1, parent_width - new_width)
                    else:
                        self.pane_widths[pane_id] = parent_width
                    return pane_id
        raise RuntimeError(f'pane not found: {parent_pane_id}')

    def respawn_pane(self, pane_id: str, *, cmd: str, cwd: str | None = None, remain_on_exit: bool = True) -> None:
        del cwd, remain_on_exit
        self.pane_options.setdefault(pane_id, {})['@respawn_cmd'] = cmd

    def _window_record(self, target: str) -> dict[str, object] | None:
        session_name, _, maybe_window = target.partition(':')
        windows = self.sessions.get(session_name, [])
        if not maybe_window:
            active_name = self.active_windows.get(session_name)
            for record in windows:
                if record['name'] == active_name:
                    return record
            return windows[0] if windows else None
        for record in windows:
            if record['name'] == maybe_window or record['id'] == maybe_window:
                return record
        return None

    def _pane_window_record(self, pane_id: str) -> tuple[str, dict[str, object]] | None:
        for session_name, windows in self.sessions.items():
            for record in windows:
                if pane_id in record['panes']:
                    return session_name, record
        return None

    def _window_visible(self, session_name: str, window_name: str) -> bool:
        key = f'{session_name}:{window_name}'
        remaining = int(self.window_visibility_lag.get(key, 0))
        if remaining <= 0:
            return True
        self.window_visibility_lag[key] = remaining - 1
        return False

    def _panes_visible(self, target: str, record: dict[str, object] | None) -> bool:
        candidates = [target]
        if record is not None:
            session_name, _, maybe_window = target.partition(':')
            candidates.append(f'{session_name}:{record["name"]}')
            candidates.append(f'{session_name}:{record["id"]}')
            if maybe_window:
                candidates.append(maybe_window)
        for key in candidates:
            remaining = int(self.pane_visibility_lag.get(key, 0))
            if remaining <= 0:
                continue
            self.pane_visibility_lag[key] = remaining - 1
            return False
        return True

    def drop_session(self, session_name: str) -> None:
        self.sessions.pop(session_name, None)
        self.active_windows.pop(session_name, None)

    def list_panes_by_user_options(self, expected: dict[str, str]) -> list[str]:
        matches = []
        for pane_id, options in self.pane_options.items():
            if all(str(options.get(key, '') or '').strip() == value for key, value in expected.items()):
                matches.append(pane_id)
        return matches

    def _format_pane(self, session_name: str, record: dict[str, object], pane_id: str, fmt: str) -> str:
        options = self.pane_options.get(pane_id, {})
        active = self.active_windows.get(session_name) == record['name'] and record['panes'][0] == pane_id
        values = {
            'session_name': session_name,
            'window_name': str(record['name']),
            'window_width': str(int(record.get('width', 160) or 160)),
            'pane_id': pane_id,
            'pane_width': str(int(self.pane_widths.get(pane_id, record.get('width', 160)) or 160)),
            'pane_active': '1' if active else '0',
        }
        rendered = fmt
        for key, value in values.items():
            rendered = rendered.replace(f'#{{{key}}}', value)
        for key, value in options.items():
            rendered = rendered.replace(f'#{{{key}}}', value)
        return rendered

    def _tmux_run(
        self,
        args: list[str],
        *,
        check: bool = False,
        capture: bool = False,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ):
        del check, input_bytes, timeout
        self.tmux_calls.append((list(args), capture))
        if args[:1] == ['start-server']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[:2] == ['set-option', '-g']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 3 and args[:2] == ['has-session', '-t']:
            return SimpleNamespace(returncode=0 if args[2] in self.sessions else 1, stdout='', stderr='')
        if len(args) >= 9 and args[:2] == ['new-session', '-d']:
            session_name = args[7]
            if '-n' in args:
                window_name = args[args.index('-n') + 1]
            else:
                window_name = session_name
            self.sessions[session_name] = []
            self.active_windows[session_name] = window_name
            self._create_window(session_name, window_name)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 7 and args[:2] == ['new-window', '-d']:
            session_name = args[args.index('-t') + 1]
            window_name = args[args.index('-n') + 1]
            self._create_window(session_name, window_name)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 4 and args[:2] == ['list-windows', '-t']:
            session_name = args[2]
            fmt = args[4] if len(args) >= 5 and args[3] == '-F' else ''
            rows = []
            for record in self.sessions.get(session_name, []):
                if not self._window_visible(session_name, str(record['name'])):
                    continue
                active = '1' if self.active_windows.get(session_name) == record['name'] else '0'
                if fmt == '#{window_name}':
                    rows.append(str(record['name']))
                else:
                    rows.append(f"{record['id']}\t{record['name']}\t{active}")
            return SimpleNamespace(returncode=0, stdout='\n'.join(rows), stderr='')
        if len(args) >= 4 and args[:2] == ['list-panes', '-a']:
            fmt = args[args.index('-F') + 1] if '-F' in args else '#{pane_id}'
            rows = []
            for session_name, windows in self.sessions.items():
                for record in windows:
                    for pane_id in record['panes']:
                        rows.append(self._format_pane(session_name, record, str(pane_id), fmt))
            return SimpleNamespace(returncode=0, stdout='\n'.join(rows), stderr='')
        if len(args) >= 4 and args[:2] == ['list-panes', '-t']:
            window = self._window_record(args[2])
            panes = list(window['panes']) if window is not None and self._panes_visible(args[2], window) else []
            if capture and len(args) >= 5 and args[4] == '#{?pane_active,#{pane_id},}':
                active = panes[0] if panes else ''
                return SimpleNamespace(returncode=0, stdout=f'{active}\n', stderr='')
            return SimpleNamespace(returncode=0, stdout='\n'.join(panes), stderr='')
        if len(args) >= 3 and args[:2] == ['select-window', '-t']:
            target = args[2]
            session_name, _, maybe_window = target.partition(':')
            if maybe_window:
                window = self._window_record(target)
                if window is not None:
                    self.active_windows[session_name] = str(window['name'])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 4 and args[:2] == ['rename-window', '-t']:
            target = args[2]
            new_name = args[3]
            window = self._window_record(target)
            if window is not None:
                session_name, _, _ = target.partition(':')
                previous_name = str(window['name'])
                window['name'] = new_name
                if self.active_windows.get(session_name) == previous_name:
                    self.active_windows[session_name] = new_name
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 3 and args[:2] == ['kill-window', '-t']:
            target = args[2]
            session_name, _, _ = target.partition(':')
            window = self._window_record(target)
            if window is not None:
                windows = self.sessions.get(session_name, [])
                self.sessions[session_name] = [record for record in windows if record is not window]
                if self.active_windows.get(session_name) == window['name']:
                    next_windows = self.sessions.get(session_name, [])
                    self.active_windows[session_name] = str(next_windows[0]['name']) if next_windows else ''
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:2] == ['set-option', '-t']:
            self.session_options.setdefault(args[2], {})[args[3]] = args[4]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:2] == ['set-option', '-u']:
            target = args[args.index('-t') + 1]
            option = args[-1]
            self.session_options.setdefault(target, {}).pop(option, None)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:2] == ['show-option', '-qv']:
            target = args[args.index('-t') + 1]
            option = args[-1]
            value = self.session_options.get(target, {}).get(option, '')
            return SimpleNamespace(returncode=0 if value else 1, stdout=f'{value}\n' if value else '', stderr='')
        if len(args) >= 5 and args[:2] == ['set-window-option', '-t']:
            self.window_options.setdefault(args[2], {})[args[3]] = args[4]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:2] == ['set-hook', '-t']:
            self.hooks.setdefault(args[2], {})[args[3]] = args[4]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 6 and args[:3] == ['set-option', '-p', '-t']:
            self.pane_options.setdefault(args[3], {})[args[4]] = args[5]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:3] == ['display-message', '-p', '-t']:
            pane_id = args[3]
            fmt = args[4]
            pane_window = self._pane_window_record(pane_id)
            if pane_window is not None:
                session_name, record = pane_window
                return SimpleNamespace(
                    returncode=0,
                    stdout=f'{self._format_pane(session_name, record, pane_id, fmt)}\n',
                    stderr='',
                )
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(args) >= 5 and args[:2] == ['resize-pane', '-t'] and '-x' in args:
            pane_id = args[2]
            width = int(args[args.index('-x') + 1])
            self.resize_calls.append((pane_id, width))
            self.pane_widths[pane_id] = width
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if args[:1] == ['kill-server']:
            self.server_killed = True
            self.sessions.clear()
            self.active_windows.clear()
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected tmux args: {args}')

    def is_alive(self, session_name: str) -> bool:
        return session_name in self.sessions

    def set_pane_title(self, pane_id: str, title: str) -> None:
        self.pane_titles[pane_id] = title

    def set_pane_user_option(self, pane_id: str, name: str, value: str) -> None:
        self.pane_options.setdefault(pane_id, {})[name] = value

    def set_pane_style(
        self,
        pane_id: str,
        *,
        border_style: str | None = None,
        active_border_style: str | None = None,
    ) -> None:
        options = self.pane_options.setdefault(pane_id, {})
        if border_style:
            options['pane-border-style'] = border_style
        if active_border_style:
            options['pane-active-border-style'] = active_border_style


def test_project_namespace_controller_creates_state_and_lifecycle_event(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-1',
        clock=lambda: '2026-04-03T02:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    namespace = controller.ensure()
    state = ProjectNamespaceStateStore(layout).load()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert namespace.project_id == 'proj-1'
    assert namespace.namespace_epoch == 1
    assert state is not None
    assert state.tmux_socket_path == str(layout.ccbd_tmux_socket_path)
    assert state.tmux_session_name == layout.ccbd_tmux_session_name
    assert state.control_window_name == layout.ccbd_tmux_control_window_name
    assert state.workspace_window_name == layout.ccbd_tmux_workspace_window_name
    assert state.workspace_epoch == 1
    assert backend.active_windows[layout.ccbd_tmux_session_name] == layout.ccbd_tmux_workspace_window_name
    assert backend.pane_titles['%2'] == 'cmd'
    assert backend.pane_options['%2']['@ccb_slot'] == 'cmd'
    assert backend.pane_options['%2']['@ccb_namespace_epoch'] == '1'
    assert backend.pane_options['%2']['@ccb_managed_by'] == 'ccbd'
    assert backend.window_options[
        f'{layout.ccbd_tmux_session_name}:{layout.ccbd_tmux_workspace_window_name}'
    ]['pane-border-status'] == 'top'
    assert 'after-select-pane' in backend.hooks[layout.ccbd_tmux_session_name]
    assert latest_event is not None
    assert latest_event.event_kind == 'namespace_created'
    assert latest_event.details['recreated'] is False
    assert latest_event.details['reason'] == 'initial_create'


def test_project_namespace_controller_materializes_explicit_windows_and_sidebar(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "review"

[windows]
main = "agent1:codex"
review = "agent2:codex, agent3:claude"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology',
        clock=lambda: '2026-04-03T02:15:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    namespace = controller.ensure(
        topology_plan=build_namespace_topology_plan(
            config,
            ccbd_socket_path=str(layout.ccbd_socket_path),
            project_root=str(project_root),
        )
    )

    windows = {
        str(record['name']): record
        for record in backend.sessions[layout.ccbd_tmux_session_name]
    }
    assert set(windows) == {'main', 'review'}
    assert namespace.workspace_window_name == 'review'
    assert backend.active_windows[layout.ccbd_tmux_session_name] == 'review'
    assert backend.pane_options['%1']['@ccb_role'] == 'sidebar'
    assert backend.pane_options['%1']['@ccb_sidebar_instance'] == 'main'
    assert backend.pane_options['%3']['@ccb_role'] == 'sidebar'
    assert backend.pane_options['%3']['@ccb_sidebar_instance'] == 'review'
    assert backend.pane_options['%2']['@ccb_slot'] == 'agent1'
    assert backend.pane_options['%4']['@ccb_slot'] == 'agent2'
    assert backend.pane_options['%5']['@ccb_slot'] == 'agent3'
    assert ('%1', 'right', 85) in backend.split_calls
    assert ('%3', 'right', 85) in backend.split_calls
    assert controller._last_materialized_agent_panes == {
        'agent1': '%2',
        'agent2': '%4',
        'agent3': '%5',
    }
    assert backend.window_options[
        f'{layout.ccbd_tmux_session_name}:main'
    ]['pane-border-status'] == 'top'
    assert backend.window_options[
        f'{layout.ccbd_tmux_session_name}:review'
    ]['pane-border-status'] == 'top'
    assert 'pane-border-format' in backend.window_options[f'{layout.ccbd_tmux_session_name}:main']
    assert 'pane-border-format' in backend.window_options[f'{layout.ccbd_tmux_session_name}:review']


def test_project_namespace_sidebar_width_preserves_agent_grid_area(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-grid'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "main"

[windows]
main = "agent1:codex, agent2:codex; agent3:codex, agent4:claude"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-grid',
        clock=lambda: '2026-04-03T02:18:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure(
        topology_plan=build_namespace_topology_plan(
            config,
            ccbd_socket_path=str(layout.ccbd_socket_path),
            project_root=str(project_root),
        )
    )

    assert backend.pane_options['%1']['@ccb_role'] == 'sidebar'
    assert backend.pane_options['%2']['@ccb_slot'] == 'agent1'
    assert backend.pane_options['%4']['@ccb_slot'] == 'agent2'
    assert backend.pane_options['%3']['@ccb_slot'] == 'agent3'
    assert backend.pane_options['%5']['@ccb_slot'] == 'agent4'
    assert backend.split_calls == [
        ('%1', 'right', 85),
        ('%2', 'right', 50),
        ('%2', 'bottom', 50),
        ('%3', 'bottom', 50),
    ]


def test_project_namespace_controller_refreshes_topology_ui_for_existing_session(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-refresh'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "review"

[windows]
main = "agent1:codex"
review = "agent2:codex, agent3:claude"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-refresh',
        clock=lambda: '2026-04-03T02:20:00Z',
        backend_factory=lambda socket_path=None: backend,
    )
    topology_plan = build_namespace_topology_plan(
        config,
        ccbd_socket_path=str(layout.ccbd_socket_path),
        project_root=str(project_root),
    )

    first = controller.ensure(topology_plan=topology_plan)
    backend.window_options[f'{layout.ccbd_tmux_session_name}:review'] = {
        'pane-border-status': 'off',
        'pane-border-format': '#{pane_index}',
    }

    second = controller.ensure(topology_plan=topology_plan)

    assert second.created_this_call is False
    assert second.namespace_epoch == first.namespace_epoch
    assert backend.window_options[
        f'{layout.ccbd_tmux_session_name}:review'
    ]['pane-border-status'] == 'top'
    assert backend.window_options[
        f'{layout.ccbd_tmux_session_name}:review'
    ]['pane-border-format'] != '#{pane_index}'


def test_project_namespace_controller_refreshes_all_sidebar_widths(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-sidebar-width-refresh'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "main"

[windows]
main = "agent1:codex"
review = "agent2:codex"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-sidebar-width-refresh',
        clock=lambda: '2026-04-03T02:22:00Z',
        backend_factory=lambda socket_path=None: backend,
    )
    topology_plan = build_namespace_topology_plan(
        config,
        ccbd_socket_path=str(layout.ccbd_socket_path),
        project_root=str(project_root),
    )

    controller.ensure(topology_plan=topology_plan)
    backend.pane_widths['%1'] = 41
    backend.pane_widths['%3'] = 23
    backend.resize_calls.clear()

    controller.ensure(topology_plan=topology_plan)

    assert backend.resize_calls == [('%1', 24), ('%3', 24)]
    assert backend.pane_widths['%1'] == 24
    assert backend.pane_widths['%3'] == 24


def test_project_namespace_controller_preserves_manual_sidebar_width_override(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-sidebar-width-override'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "main"

[windows]
main = "agent1:codex"
review = "agent2:codex"

[ui.sidebar]
mode = "every_window"
width = "15%"
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-sidebar-width-override',
        clock=lambda: '2026-04-03T02:22:30Z',
        backend_factory=lambda socket_path=None: backend,
    )
    topology_plan = build_namespace_topology_plan(
        config,
        ccbd_socket_path=str(layout.ccbd_socket_path),
        project_root=str(project_root),
    )

    controller.ensure(topology_plan=topology_plan)
    backend.pane_widths['%1'] = 41
    backend.pane_widths['%3'] = 23
    backend.session_options.setdefault(layout.ccbd_tmux_session_name, {})['@ccb_sidebar_width_cells'] = '41'
    backend.resize_calls.clear()

    controller.ensure(topology_plan=topology_plan)

    assert backend.resize_calls == [('%3', 41)]
    assert backend.pane_widths['%1'] == 41
    assert backend.pane_widths['%3'] == 41


def test_project_namespace_sidebar_integer_width_uses_columns(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-sidebar-integer-width'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "main"

[windows]
main = "agent1:codex"

[ui.sidebar]
mode = "every_window"
width = 30
bottom_height = 20
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-sidebar-integer-width',
        clock=lambda: '2026-04-03T02:23:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure(
        topology_plan=build_namespace_topology_plan(
            config,
            ccbd_socket_path=str(layout.ccbd_socket_path),
            project_root=str(project_root),
        )
    )

    assert backend.split_calls[0] == ('%1', 'right', 81)
    assert backend.pane_widths['%1'] == 30


def test_project_namespace_controller_clears_topology_panes_when_reusing_without_topology(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-topology-clear'
    (project_root / '.ccb').mkdir(parents=True)
    (project_root / '.ccb' / 'ccb.config').write_text(
        """version = 2
entry_window = "main"

[windows]
main = "agent1:codex"
work = "agent2:codex"
""",
        encoding='utf-8',
    )
    config = load_project_config(project_root).config
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-topology-clear',
        clock=lambda: '2026-04-03T02:25:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure(
        topology_plan=build_namespace_topology_plan(
            config,
            ccbd_socket_path=str(layout.ccbd_socket_path),
            project_root=str(project_root),
        )
    )
    assert controller._last_materialized_agent_panes

    namespace = controller.ensure()

    assert namespace.created_this_call is False
    assert controller._last_materialized_agent_panes == {}
    assert controller._last_topology_active_panes == ()


def test_project_namespace_controller_applies_server_policy_when_reusing_session(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-reuse-policy'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-reuse-policy',
        clock=lambda: '2026-04-03T02:30:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure()
    backend.tmux_calls.clear()
    namespace = controller.ensure()

    assert namespace.created_this_call is False
    assert (['set-option', '-g', 'destroy-unattached', 'off'], True) in backend.tmux_calls
    assert (['set-option', '-g', 'mouse', 'on'], True) in backend.tmux_calls
    assert (['set-option', '-g', 'set-clipboard', 'on'], True) in backend.tmux_calls


def test_prepare_server_preserves_tmux_failure_detail_for_diagnostics(tmp_path: Path) -> None:
    socket_path = tmp_path / 'repo' / '.ccb' / 'ccbd' / 'tmux.sock'

    class _FailingStartServerBackend(_FakeTmuxBackend):
        def __init__(self) -> None:
            super().__init__(socket_path=str(socket_path))
            self._socket_path = str(socket_path)

        def _tmux_base(self) -> list[str]:
            return ['tmux', '-f', '/dev/null', '-S', self._socket_path]

        def _tmux_run(
            self,
            args: list[str],
            *,
            check: bool = False,
            capture: bool = False,
            input_bytes: bytes | None = None,
            timeout: float | None = None,
        ):
            del check, capture, input_bytes, timeout
            if args[:1] == ['start-server']:
                return SimpleNamespace(
                    returncode=1,
                    stdout='',
                    stderr='error connecting to /private/tmp/tmux-501/default (No such file or directory)\n',
                )
            return super()._tmux_run(args, check=False, capture=True)

    try:
        prepare_server(_FailingStartServerBackend())
    except RuntimeError as exc:
        text = str(exc)
    else:
        raise AssertionError('expected prepare_server to fail')

    assert 'failed to prepare tmux server' in text
    assert f'tmux_socket_path={socket_path}' in text
    assert 'tmux_socket_path_bytes=' in text
    assert "tmux_command='tmux -f /dev/null -S" in text
    assert 'start-server' in text
    assert 'No such file or directory' in text


def test_project_namespace_controller_recreates_missing_session_with_new_epoch(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-recreate'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-2',
        clock=lambda: '2026-04-03T03:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    first = controller.ensure()
    backend.drop_session(layout.ccbd_tmux_session_name)
    second = controller.ensure()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert first.namespace_epoch == 1
    assert second.namespace_epoch == 2
    assert latest_event is not None
    assert latest_event.event_kind == 'namespace_created'
    assert latest_event.namespace_epoch == 2
    assert latest_event.details['recreated'] is True
    assert latest_event.details['reason'] == 'missing_session'


def test_project_namespace_controller_recreates_after_kill_when_has_session_reports_no_server_running(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-recreate-no-server'
    layout = PathLayout(project_root)

    class _NoServerWhenAbsentBackend(_FakeTmuxBackend):
        def _tmux_run(
            self,
            args: list[str],
            *,
            check: bool = False,
            capture: bool = False,
            input_bytes: bytes | None = None,
            timeout: float | None = None,
        ):
            if len(args) >= 3 and args[:2] == ['has-session', '-t'] and args[2] not in self.sessions:
                self.tmux_calls.append((list(args), capture))
                return SimpleNamespace(
                    returncode=1,
                    stdout='',
                    stderr=f'no server running on {layout.ccbd_tmux_socket_path}\n',
                )
            return super()._tmux_run(
                args,
                check=check,
                capture=capture,
                input_bytes=input_bytes,
                timeout=timeout,
            )

    backend = _NoServerWhenAbsentBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-2b',
        clock=lambda: '2026-04-03T03:30:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    first = controller.ensure()
    controller.destroy(reason='kill')
    second = controller.ensure()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert first.namespace_epoch == 1
    assert second.namespace_epoch == 2
    assert second.ui_attachable is True
    assert layout.ccbd_tmux_session_name in backend.sessions
    assert latest_event is not None
    assert latest_event.event_kind == 'namespace_created'
    assert latest_event.namespace_epoch == 2
    assert latest_event.details['reason'] == 'missing_session'


def test_project_namespace_controller_recreates_session_when_layout_version_changes(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-layout-upgrade'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    state_store = ProjectNamespaceStateStore(layout)
    state_store.save(
        ProjectNamespaceState(
            project_id='proj-5',
            namespace_epoch=4,
            tmux_socket_path=str(layout.ccbd_tmux_socket_path),
            tmux_session_name=layout.ccbd_tmux_session_name,
            layout_version=1,
            layout_signature='cmd; agent1:codex',
            ui_attachable=True,
        )
    )
    backend.sessions[layout.ccbd_tmux_session_name] = [{'id': '@8', 'name': layout.ccbd_tmux_workspace_window_name, 'panes': ['%8']}]
    backend.active_windows[layout.ccbd_tmux_session_name] = layout.ccbd_tmux_workspace_window_name
    controller = ProjectNamespaceController(
        layout,
        'proj-5',
        clock=lambda: '2026-04-03T06:00:00Z',
        backend_factory=lambda socket_path=None: backend,
        layout_version=3,
    )

    namespace = controller.ensure()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert namespace.namespace_epoch == 5
    assert backend.server_killed is True
    assert backend.pane_titles['%2'] == 'cmd'
    assert latest_event is not None
    assert latest_event.details['reason'] == 'layout_version_changed'


def test_project_namespace_controller_recreates_session_when_layout_signature_changes(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-layout-signature-upgrade'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    state_store = ProjectNamespaceStateStore(layout)
    state_store.save(
        ProjectNamespaceState(
            project_id='proj-6',
            namespace_epoch=7,
            tmux_socket_path=str(layout.ccbd_tmux_socket_path),
            tmux_session_name=layout.ccbd_tmux_session_name,
            layout_version=3,
            layout_signature='cmd; agent1:codex',
            ui_attachable=True,
        )
    )
    backend.sessions[layout.ccbd_tmux_session_name] = [{'id': '@9', 'name': layout.ccbd_tmux_workspace_window_name, 'panes': ['%9']}]
    backend.active_windows[layout.ccbd_tmux_session_name] = layout.ccbd_tmux_workspace_window_name
    controller = ProjectNamespaceController(
        layout,
        'proj-6',
        clock=lambda: '2026-04-03T07:00:00Z',
        backend_factory=lambda socket_path=None: backend,
        layout_version=3,
    )

    namespace = controller.ensure(layout_signature='cmd, agent1:codex; agent2:claude')
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert namespace.namespace_epoch == 8
    assert namespace.layout_signature == 'cmd, agent1:codex; agent2:claude'
    assert backend.server_killed is True
    assert backend.pane_titles['%2'] == 'cmd'
    assert latest_event is not None
    assert latest_event.details['reason'] == 'layout_signature_changed'


def test_project_namespace_controller_waits_for_delayed_window_and_pane_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / 'repo-delayed-namespace-visibility'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    backend.window_visibility_lag[f'{layout.ccbd_tmux_session_name}:{layout.ccbd_tmux_workspace_window_name}'] = 2
    backend.pane_visibility_lag[f'{layout.ccbd_tmux_session_name}:{layout.ccbd_tmux_workspace_window_name}'] = 2
    controller = ProjectNamespaceController(
        layout,
        'proj-delay-1',
        clock=lambda: '2026-04-03T07:30:00Z',
        backend_factory=lambda socket_path=None: backend,
    )
    monkeypatch.setenv('CCB_TMUX_OBJECT_READY_TIMEOUT_S', '0.2')
    monkeypatch.setenv('CCB_TMUX_OBJECT_READY_POLL_INTERVAL_S', '0')

    namespace = controller.ensure()
    state = ProjectNamespaceStateStore(layout).load()

    assert namespace.workspace_window_name == layout.ccbd_tmux_workspace_window_name
    assert state is not None
    assert state.workspace_window_name == layout.ccbd_tmux_workspace_window_name
    assert backend.pane_titles['%2'] == 'cmd'


def test_project_namespace_controller_destroy_marks_state_and_event(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-destroy'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-3',
        clock=lambda: '2026-04-03T04:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure()
    summary = controller.destroy(reason='kill')
    state = ProjectNamespaceStateStore(layout).load()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert summary.destroyed is True
    assert summary.reason == 'kill'
    assert backend.server_killed is True
    assert state is not None
    assert state.ui_attachable is False
    assert state.last_destroy_reason == 'kill'
    assert latest_event is not None
    assert latest_event.event_kind == 'namespace_destroyed'
    assert latest_event.details['reason'] == 'kill'


def test_project_namespace_controller_reflows_workspace_without_killing_server(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-reflow-workspace'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-7',
        clock=lambda: '2026-04-03T08:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    first = controller.ensure()
    namespace = controller.reflow_workspace(
        layout_signature='cmd; agent1:codex',
        reason='pane_recovery:agent1',
    )
    state = ProjectNamespaceStateStore(layout).load()
    latest_event = ProjectNamespaceEventStore(layout).load_latest()

    assert first.namespace_epoch == 1
    assert namespace.namespace_epoch == 1
    assert namespace.workspace_epoch == 2
    assert namespace.workspace_recreated_this_call is True
    assert backend.server_killed is False
    assert state is not None
    assert state.control_window_id == '@1'
    assert state.workspace_window_id == '@3'
    assert backend.active_windows[layout.ccbd_tmux_session_name] == layout.ccbd_tmux_workspace_window_name
    assert backend.pane_titles['%3'] == 'cmd'
    assert latest_event is not None
    assert latest_event.event_kind == 'workspace_reflowed'
    assert latest_event.details['reason'] == 'pane_recovery:agent1'


def test_project_namespace_controller_reflow_waits_for_renamed_workspace_visibility(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / 'repo-reflow-delayed-visibility'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-reflow-delay',
        clock=lambda: '2026-04-03T08:30:00Z',
        backend_factory=lambda socket_path=None: backend,
    )
    controller.ensure()
    backend.window_visibility_lag[f'{layout.ccbd_tmux_session_name}:{layout.ccbd_tmux_workspace_window_name}'] = 2
    monkeypatch.setenv('CCB_TMUX_OBJECT_READY_TIMEOUT_S', '0.2')
    monkeypatch.setenv('CCB_TMUX_OBJECT_READY_POLL_INTERVAL_S', '0')

    namespace = controller.reflow_workspace(
        layout_signature='cmd; agent1:codex',
        reason='pane_recovery:agent1',
    )

    assert namespace.workspace_epoch == 2
    assert backend.active_windows[layout.ccbd_tmux_session_name] == layout.ccbd_tmux_workspace_window_name


def test_project_namespace_reflow_targets_transient_window_by_id(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-reflow-targets'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-8',
        clock=lambda: '2026-04-03T09:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure()
    backend.tmux_calls.clear()

    controller.reflow_workspace(
        layout_signature='cmd; agent1:codex',
        reason='pane_recovery:agent1',
    )

    targeted = [
        args
        for args, _capture in backend.tmux_calls
        if args[:1] and args[0] in {'select-window', 'rename-window', 'kill-window'}
    ]

    assert targeted
    for args in targeted:
        target = args[2]
        assert '.__reflow__.' not in target
        assert target.startswith(f'{layout.ccbd_tmux_session_name}:@')


def test_project_namespace_controller_uses_silent_server_commands(tmp_path: Path) -> None:
    project_root = tmp_path / 'repo-silent'
    layout = PathLayout(project_root)
    backend = _FakeTmuxBackend()
    controller = ProjectNamespaceController(
        layout,
        'proj-4',
        clock=lambda: '2026-04-03T05:00:00Z',
        backend_factory=lambda socket_path=None: backend,
    )

    controller.ensure()
    controller.destroy(reason='kill')

    new_session_calls = [args for args, _capture in backend.tmux_calls if args[:2] == ['new-session', '-d']]
    assert len(new_session_calls) == 1
    assert new_session_calls[0][-3:] == ['sh', '-lc', 'while :; do sleep 3600; done']
    assert (['start-server'], True) in backend.tmux_calls
    assert (['set-option', '-g', 'destroy-unattached', 'off'], True) in backend.tmux_calls
    assert (['set-option', '-g', 'mouse', 'on'], True) in backend.tmux_calls
    assert (['set-option', '-g', 'set-clipboard', 'on'], True) in backend.tmux_calls
    assert (['kill-server'], True) in backend.tmux_calls
