from __future__ import annotations

from pathlib import Path

from cli.sidebar_click import SidebarClick, focus_sidebar_click, sidebar_tree_targets


SAMPLE_VIEW = {
    'namespace': {'epoch': 7},
    'windows': [
        {'name': 'main'},
        {'name': 'work'},
        {'name': 'review'},
    ],
    'agents': [
        {'name': 'agent1', 'window': 'main'},
        {'name': 'agent2', 'window': 'main'},
        {'name': 'agent3', 'window': 'work'},
        {'name': 'agent4', 'window': 'review'},
    ],
}


class FakeClient:
    calls: list[tuple[str, str, int | None]] = []

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def project_view(self, *, schema_version: int) -> dict:
        assert schema_version == 1
        return {'view': SAMPLE_VIEW}

    def project_focus_window(self, window: str, *, namespace_epoch: int | None = None) -> dict:
        self.calls.append(('window', window, namespace_epoch))
        return {}

    def project_focus_agent(self, agent: str, *, namespace_epoch: int | None = None) -> dict:
        self.calls.append(('agent', agent, namespace_epoch))
        return {}


def test_sidebar_tree_targets_match_sidebar_render_order() -> None:
    assert sidebar_tree_targets(SAMPLE_VIEW) == [
        ('window', 'main'),
        ('agent', 'agent1'),
        ('agent', 'agent2'),
        ('window', 'work'),
        ('agent', 'agent3'),
        ('window', 'review'),
        ('agent', 'agent4'),
    ]


def test_sidebar_click_focuses_window_from_pane_relative_tmux_row() -> None:
    FakeClient.calls = []

    target = focus_sidebar_click(
        SidebarClick(socket_path=Path('/tmp/ccbd.sock'), mouse_y=4, pane_top=1, pane_height=47),
        client_factory=FakeClient,
    )

    assert target == 'window:work'
    assert FakeClient.calls == [('window', 'work', 7)]


def test_sidebar_click_focuses_agent_from_second_agent_row() -> None:
    FakeClient.calls = []

    target = focus_sidebar_click(
        SidebarClick(socket_path=Path('/tmp/ccbd.sock'), mouse_y=3, pane_top=1, pane_height=47),
        client_factory=FakeClient,
    )

    assert target == 'agent:agent2'
    assert FakeClient.calls == [('agent', 'agent2', 7)]


def test_sidebar_click_accepts_absolute_tmux_row_when_outside_pane_relative_range() -> None:
    FakeClient.calls = []

    target = focus_sidebar_click(
        SidebarClick(socket_path=Path('/tmp/ccbd.sock'), mouse_y=52, pane_top=48, pane_height=47),
        client_factory=FakeClient,
    )

    assert target == 'window:work'
    assert FakeClient.calls == [('window', 'work', 7)]


def test_sidebar_click_ignores_title_border_and_empty_rows() -> None:
    FakeClient.calls = []

    title = focus_sidebar_click(
        SidebarClick(socket_path=Path('/tmp/ccbd.sock'), mouse_y=0, pane_top=1, pane_height=47),
        client_factory=FakeClient,
    )
    empty = focus_sidebar_click(
        SidebarClick(socket_path=Path('/tmp/ccbd.sock'), mouse_y=20, pane_top=1, pane_height=47),
        client_factory=FakeClient,
    )

    assert title is None
    assert empty is None
    assert FakeClient.calls == []
