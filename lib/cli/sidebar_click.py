from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ccbd.socket_client import CcbdClient


@dataclass(frozen=True)
class SidebarClick:
    socket_path: Path
    mouse_y: int
    pane_top: int
    pane_height: int


def maybe_handle_sidebar_click_command(tokens: list[str], *, stderr: TextIO) -> int | None:
    if not tokens or tokens[0] != '__sidebar-click':
        return None
    try:
        click = _parse_sidebar_click(tokens[1:])
        focus_sidebar_click(click)
        return 0
    except Exception as exc:
        print(f'ccb sidebar click failed: {exc}', file=stderr)
        return 1


def focus_sidebar_click(click: SidebarClick, *, client_factory=CcbdClient) -> str | None:
    relative_y = _relative_coordinate(click.mouse_y, click.pane_top, click.pane_height)
    if relative_y <= 0 or relative_y >= max(1, click.pane_height - 1):
        return None
    row_index = relative_y - 1
    client = client_factory(click.socket_path)
    view_payload = client.project_view(schema_version=1)
    view = view_payload.get('view') if isinstance(view_payload, dict) else None
    if not isinstance(view, dict):
        return None
    targets = sidebar_tree_targets(view)
    if row_index < 0 or row_index >= len(targets):
        return None
    kind, name = targets[row_index]
    namespace = view.get('namespace') if isinstance(view.get('namespace'), dict) else {}
    namespace_epoch = namespace.get('epoch') if isinstance(namespace, dict) else None
    if kind == 'window':
        client.project_focus_window(name, namespace_epoch=namespace_epoch)
    else:
        client.project_focus_agent(name, namespace_epoch=namespace_epoch)
    return f'{kind}:{name}'


def sidebar_tree_targets(view: dict) -> list[tuple[str, str]]:
    windows = view.get('windows') if isinstance(view.get('windows'), list) else []
    agents = view.get('agents') if isinstance(view.get('agents'), list) else []
    targets: list[tuple[str, str]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        window_name = str(window.get('name') or '').strip()
        if not window_name:
            continue
        targets.append(('window', window_name))
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if str(agent.get('window') or '').strip() != window_name:
                continue
            agent_name = str(agent.get('name') or '').strip()
            if agent_name:
                targets.append(('agent', agent_name))
    return targets


def _parse_sidebar_click(argv: list[str]) -> SidebarClick:
    parser = argparse.ArgumentParser(prog='ccb __sidebar-click', add_help=False)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--mouse-y', required=True, type=int)
    parser.add_argument('--pane-top', required=True, type=int)
    parser.add_argument('--pane-height', required=True, type=int)
    args = parser.parse_args(argv)
    return SidebarClick(
        socket_path=Path(args.socket),
        mouse_y=int(args.mouse_y),
        pane_top=int(args.pane_top),
        pane_height=int(args.pane_height),
    )


def _relative_coordinate(value: int, pane_start: int, pane_size: int) -> int:
    # tmux normally exposes pane-relative mouse coordinates for pane bindings.
    # Keep an absolute-coordinate fallback for older or unusual format contexts.
    if value >= pane_size and value >= pane_start:
        return value - pane_start
    return value


__all__ = [
    'SidebarClick',
    'focus_sidebar_click',
    'maybe_handle_sidebar_click_command',
    'sidebar_tree_targets',
]
