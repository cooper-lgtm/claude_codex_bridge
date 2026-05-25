from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cli.sidebar_resize_sync import SidebarResizeSync, sync_sidebar_resize


def test_sidebar_resize_sync_copies_source_window_sidebar_width_to_other_windows() -> None:
    calls: list[list[str]] = []
    pane_rows = '\n'.join(
        [
            'ccb-demo\t@1\tmain\t%0\t41\t160\tproj-1\tsidebar\tmain\tccbd',
            'ccb-demo\t@1\tmain\t%1\t118\t160\tproj-1\tagent\t\tccbd',
            'ccb-demo\t@2\twork\t%2\t23\t160\tproj-1\tsidebar\twork\tccbd',
            'ccb-demo\t@2\twork\t%3\t136\t160\tproj-1\tagent\t\tccbd',
            'ccb-demo\t@3\treview\t%4\t24\t160\tproj-1\tsidebar\treview\tccbd',
            'ccb-demo\t@3\treview\t%5\t135\t160\tproj-1\tagent\t\tccbd',
        ]
    )

    def fake_run(args, **kwargs):
        del kwargs
        tmux_args = _tmux_command_args(args)
        calls.append(tmux_args)
        if tmux_args[:2] == ['list-panes', '-a']:
            return SimpleNamespace(returncode=0, stdout=pane_rows, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    count = sync_sidebar_resize(
        SidebarResizeSync(
            tmux_socket_path=Path('/tmp/tmux.sock'),
            session_name='ccb-demo',
            source_pane='%1',
            project_id='proj-1',
        ),
        run_fn=fake_run,
    )

    assert count == 2
    assert ['set-option', '-t', 'ccb-demo', '@ccb_sidebar_width_cells', '41'] in calls
    assert ['set-option', '-t', 'ccb-demo', '@ccb_sidebar_sync_guard', '1'] in calls
    assert ['resize-pane', '-t', '%2', '-x', '41'] in calls
    assert ['resize-pane', '-t', '%4', '-x', '41'] in calls
    assert ['set-option', '-u', '-t', 'ccb-demo', '@ccb_sidebar_sync_guard'] in calls


def test_sidebar_resize_sync_noops_when_source_window_has_no_sidebar() -> None:
    calls: list[list[str]] = []
    pane_rows = 'ccb-demo\t@1\tmain\t%1\t160\t160\tproj-1\tagent\t\tccbd'

    def fake_run(args, **kwargs):
        del kwargs
        tmux_args = _tmux_command_args(args)
        calls.append(tmux_args)
        if tmux_args[:2] == ['list-panes', '-a']:
            return SimpleNamespace(returncode=0, stdout=pane_rows, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    count = sync_sidebar_resize(
        SidebarResizeSync(
            tmux_socket_path=Path('/tmp/tmux.sock'),
            session_name='ccb-demo',
            source_pane='%1',
            project_id='proj-1',
        ),
        run_fn=fake_run,
    )

    assert count is None
    assert len(calls) == 1
    assert calls[0][:3] == ['list-panes', '-a', '-F']


def test_sidebar_resize_sync_reapplies_stored_width_after_window_resize() -> None:
    calls: list[list[str]] = []
    pane_rows = '\n'.join(
        [
            'ccb-demo\t@1\tmain\t%0\t19\t80\tproj-1\tsidebar\tmain\tccbd',
            'ccb-demo\t@1\tmain\t%1\t60\t80\tproj-1\tagent\t\tccbd',
            'ccb-demo\t@2\twork\t%2\t59\t160\tproj-1\tsidebar\twork\tccbd',
            'ccb-demo\t@2\twork\t%3\t100\t160\tproj-1\tagent\t\tccbd',
        ]
    )

    def fake_run(args, **kwargs):
        del kwargs
        tmux_args = _tmux_command_args(args)
        calls.append(tmux_args)
        if tmux_args[:2] == ['list-panes', '-a']:
            return SimpleNamespace(returncode=0, stdout=pane_rows, stderr='')
        if tmux_args[:4] == ['show-option', '-qv', '-t', 'ccb-demo']:
            return SimpleNamespace(returncode=0, stdout='59\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    count = sync_sidebar_resize(
        SidebarResizeSync(
            tmux_socket_path=Path('/tmp/tmux.sock'),
            session_name='ccb-demo',
            source_window='@1',
            from_stored_width=True,
        ),
        run_fn=fake_run,
    )

    assert count == 1
    assert ['show-option', '-qv', '-t', 'ccb-demo', '@ccb_sidebar_width_cells'] in calls
    assert ['set-option', '-t', 'ccb-demo', '@ccb_sidebar_width_cells', '59'] not in calls
    assert ['resize-pane', '-t', '%0', '-x', '59'] in calls
    assert ['set-option', '-t', 'ccb-demo', '@ccb_sidebar_sync_guard', '1'] in calls
    assert ['set-option', '-u', '-t', 'ccb-demo', '@ccb_sidebar_sync_guard'] in calls


def _tmux_command_args(args) -> list[str]:
    raw = list(args)
    for index, value in enumerate(raw):
        if value in {'list-panes', 'resize-pane', 'set-option', 'show-option'}:
            return raw[index:]
    return raw
