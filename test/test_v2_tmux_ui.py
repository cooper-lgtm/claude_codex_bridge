from __future__ import annotations

from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import cli.services.tmux_ui as tmux_ui
import cli.services.tmux_ui_runtime.helpers as tmux_helpers


def test_set_tmux_ui_active_runs_expected_script_from_current_install_root(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / 'config'
    config_dir.mkdir(parents=True)
    on_script = config_dir / 'ccb-tmux-on.sh'
    off_script = config_dir / 'ccb-tmux-off.sh'
    on_script.write_text('#!/bin/sh\n', encoding='utf-8')
    off_script.write_text('#!/bin/sh\n', encoding='utf-8')

    calls: list[list[str]] = []

    monkeypatch.setenv('TMUX', '/tmp/tmux-1/default,123,0')
    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: tmp_path)
    monkeypatch.setattr(tmux_ui.subprocess, 'run', lambda args, **kwargs: calls.append(list(args)))

    tmux_ui.set_tmux_ui_active(True)
    tmux_ui.set_tmux_ui_active(False)

    assert calls == [[str(on_script)], [str(off_script)]]


def test_set_tmux_ui_active_skips_outside_tmux(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    monkeypatch.delenv('TMUX', raising=False)
    monkeypatch.delenv('TMUX_PANE', raising=False)
    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: tmp_path)
    monkeypatch.setattr(tmux_ui.subprocess, 'run', lambda args, **kwargs: calls.append(list(args)))

    tmux_ui.set_tmux_ui_active(True)

    assert calls == []


def test_set_tmux_ui_active_falls_back_to_path_lookup(monkeypatch, tmp_path: Path) -> None:
    path_dir = tmp_path / 'path-bin'
    path_dir.mkdir(parents=True)
    on_script = path_dir / 'ccb-tmux-on.sh'
    on_script.write_text('#!/bin/sh\n', encoding='utf-8')

    calls: list[list[str]] = []

    monkeypatch.setenv('TMUX', '/tmp/tmux-1/default,123,0')
    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: tmp_path / 'missing-root')
    monkeypatch.setattr(tmux_helpers.shutil, 'which', lambda name: str(on_script) if name == 'ccb-tmux-on.sh' else None)
    monkeypatch.setattr(tmux_ui.subprocess, 'run', lambda args, **kwargs: calls.append(list(args)))

    tmux_ui.set_tmux_ui_active(True)

    assert calls == [[str(on_script)]]


def test_apply_project_tmux_ui_sets_session_theme_and_hook_from_current_install_root(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / 'config'
    config_dir.mkdir(parents=True)
    for script_name in ('ccb-status.sh', 'ccb-border.sh', 'ccb-git.sh'):
        (config_dir / script_name).write_text('#!/bin/sh\n', encoding='utf-8')
    (tmp_path / 'VERSION').write_text('9.9.9\n', encoding='utf-8')

    calls: list[list[str]] = []

    class FakeBackend:
        def _tmux_run(self, args, *, check=False, capture=False):
            del check
            calls.append(list(args))
            if capture and args[:4] == ['list-panes', '-t', 'ccb-demo', '-F']:
                return SimpleNamespace(returncode=0, stdout='\n%9\n', stderr='')
            if capture and args[:3] == ['list-panes', '-a', '-F']:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'ccb-demo\tmain\t%8\t0\tsidebar\tfg=#6c7086\tfg=#6c7086\n'
                        'ccb-demo\tmain\t%9\t1\tagent\tfg=#f7768e\tfg=#f7768e,bold\n'
                    ),
                    stderr='',
                )
            if capture and args[:4] == ['display-message', '-p', '-t', '%9']:
                if args[4] == '#{@ccb_role}':
                    return SimpleNamespace(returncode=0, stdout='agent\n', stderr='')
                if args[4] == '#{@ccb_active_border_style}':
                    return SimpleNamespace(returncode=0, stdout='fg=#f7768e,bold\n', stderr='')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            if capture and args[:4] == ['list-windows', '-t', 'ccb-demo', '-F']:
                return SimpleNamespace(returncode=0, stdout='main\nreview\n', stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: tmp_path)

    tmux_ui.apply_project_tmux_ui(
        tmux_socket_path='/tmp/ccb.sock',
        ccbd_socket_path='/tmp/ccbd.sock',
        tmux_session_name='ccb-demo',
        backend=FakeBackend(),
    )

    assert ['set-option', '-t', 'ccb-demo', '@ccb_version', '9.9.9'] in calls
    assert ['set-window-option', '-t', 'ccb-demo:main', 'pane-border-status', 'top'] in calls
    assert ['set-window-option', '-t', 'ccb-demo:review', 'pane-border-status', 'top'] in calls
    assert ['set-window-option', '-t', 'ccb-demo:main', 'pane-border-style', 'fg=#f7768e'] in calls
    assert ['set-window-option', '-t', 'ccb-demo:main', 'pane-active-border-style', 'fg=#f7768e,bold'] in calls
    assert any(
        call[:4] == ['set-window-option', '-t', 'ccb-demo:main', 'pane-border-format']
        for call in calls
    )
    assert any(
        call[:4] == ['set-window-option', '-t', 'ccb-demo:review', 'pane-border-format']
        for call in calls
    )
    assert any(
        call[:4] == ['set-hook', '-t', 'ccb-demo', 'after-select-pane']
        and 'ccb-border.sh' in call[4]
        for call in calls
    )
    sidebar_mouse_bindings = [
        call for call in calls if call[:4] == ['bind-key', '-T', 'root', 'MouseDown1Pane']
    ]
    assert len(sidebar_mouse_bindings) == 1
    sidebar_mouse_binding = sidebar_mouse_bindings[0]
    assert '#{@ccb_role},sidebar' in sidebar_mouse_binding[8]
    assert '#{mouse_y}' in sidebar_mouse_binding[9]
    assert 'start=$((width - 4))' in sidebar_mouse_binding[9]
    assert 'send-keys -t "$pane" -l R' in sidebar_mouse_binding[9]
    assert 'send-keys -t "$pane" -l Q' in sidebar_mouse_binding[9]
    assert '__sidebar-click' in sidebar_mouse_binding[9]
    assert '--socket /tmp/ccbd.sock' in sidebar_mouse_binding[9]
    assert '--mouse-y "$y" --pane-top "$top" --pane-height "$height"' in sidebar_mouse_binding[9]
    assert 'send-keys -M' not in sidebar_mouse_binding[9]
    assert sidebar_mouse_binding[10] == 'select-pane -t = \\; send-keys -M'
    sidebar_resize_bindings = [
        call for call in calls if call[:4] == ['bind-key', '-T', 'root', 'MouseDrag1Border']
    ]
    assert len(sidebar_resize_bindings) == 1
    sidebar_resize_binding = sidebar_resize_bindings[0]
    assert sidebar_resize_binding == ['bind-key', '-T', 'root', 'MouseDrag1Border', 'resize-pane', '-M']
    sidebar_resize_hooks = [
        call for call in calls if call[:4] == ['set-hook', '-t', 'ccb-demo', 'after-resize-pane']
    ]
    assert len(sidebar_resize_hooks) == 1
    sidebar_resize_hook = sidebar_resize_hooks[0][4]
    assert '__sidebar-resize-sync' in sidebar_resize_hook
    assert '@ccb_sidebar_sync_guard' in sidebar_resize_hook
    assert '--tmux-socket /tmp/ccb.sock' in sidebar_resize_hook
    assert '--session ccb-demo' in sidebar_resize_hook
    assert '--source-pane "#{pane_id}"' in sidebar_resize_hook
    assert '--project-id "#{@ccb_project_id}"' in sidebar_resize_hook
    sidebar_window_resize_hooks = [
        call for call in calls if call[:3] == ['set-hook', '-g', 'window-resized']
    ]
    assert len(sidebar_window_resize_hooks) == 1
    sidebar_window_resize_hook = sidebar_window_resize_hooks[0][3]
    assert '__sidebar-resize-sync' in sidebar_window_resize_hook
    assert '@ccb_sidebar_sync_guard' in sidebar_window_resize_hook
    assert 'current_session="#{session_name}"' in sidebar_window_resize_hook
    assert '--source-window "#{window_id}"' in sidebar_window_resize_hook
    assert '--from-stored-width' in sidebar_window_resize_hook
    assert ['set-option', '-p', '-t', '%9', 'pane-active-border-style', 'fg=#f7768e,bold'] in calls


def test_apply_project_tmux_ui_applies_window_theme_for_contrast_profile(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / 'config'
    config_dir.mkdir(parents=True)
    for script_name in ('ccb-status.sh', 'ccb-border.sh', 'ccb-git.sh'):
        (config_dir / script_name).write_text('#!/bin/sh\n', encoding='utf-8')
    (tmp_path / 'VERSION').write_text('9.9.9\n', encoding='utf-8')

    calls: list[list[str]] = []

    class FakeBackend:
        def _tmux_run(self, args, *, check=False, capture=False):
            del check
            calls.append(list(args))
            if capture and args[:4] == ['list-panes', '-t', 'ccb-demo', '-F']:
                return SimpleNamespace(returncode=0, stdout='\n%9\n', stderr='')
            if capture and args[:4] == ['display-message', '-p', '-t', '%9']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setenv('CCB_TMUX_THEME_PROFILE', 'contrast')
    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: tmp_path)

    tmux_ui.apply_project_tmux_ui(
        tmux_socket_path='/tmp/ccb.sock',
        tmux_session_name='ccb-demo',
        backend=FakeBackend(),
    )

    assert ['set-option', '-t', 'ccb-demo', '@ccb_theme_profile', 'contrast'] in calls
    assert ['set-window-option', '-t', 'ccb-demo', 'pane-border-style', 'fg=#565f89,bold'] in calls
    assert ['set-window-option', '-t', 'ccb-demo', 'window-style', 'bg=#181825'] in calls
    assert ['set-window-option', '-t', 'ccb-demo', 'window-active-style', 'bg=#1e1e2e'] in calls


def test_border_script_keeps_sidebar_active_border_gray(tmp_path: Path) -> None:
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    log_path = tmp_path / 'tmux.log'
    fake_tmux = fake_bin / 'tmux'
    fake_tmux.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {log_path}
if [[ "$1 $2 $3 $4" == "display-message -p -t %0" ]]; then
  case "$5" in
    "#{{@ccb_active_border_style}}") printf '%s\\n' 'fg=#6c7086' ;;
    "#{{@ccb_border_style}}") printf '%s\\n' 'fg=#6c7086' ;;
    "#{{@ccb_role}}") printf '%s\\n' 'sidebar' ;;
    "#{{session_name}}:#{{window_name}}") printf '%s\\n' 'ccb-demo:main' ;;
    *) printf '\\n' ;;
  esac
fi
""",
        encoding='utf-8',
    )
    fake_tmux.chmod(0o755)

    proc = subprocess.run(
        ['bash', str(Path('config/ccb-border.sh').resolve()), '%0'],
        env={**os.environ, 'PATH': f'{fake_bin}:{os.environ.get("PATH", "")}'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    calls = log_path.read_text(encoding='utf-8')
    assert 'list-panes' not in calls
    assert 'set-option -p -t %0 pane-active-border-style fg=#6c7086' in calls


def test_detect_ccb_version_prefers_current_install_over_path(monkeypatch, tmp_path: Path) -> None:
    current_root = tmp_path / 'current'
    current_root.mkdir()
    (current_root / 'VERSION').write_text('9.9.9\n', encoding='utf-8')

    path_root = tmp_path / 'path-root'
    path_root.mkdir()
    path_ccb = path_root / 'ccb'
    path_ccb.write_text('VERSION = "1.2.3"\n', encoding='utf-8')

    monkeypatch.delenv('CCB_VERSION', raising=False)
    monkeypatch.setattr(tmux_helpers, 'current_install_root', lambda: current_root)
    monkeypatch.setattr(tmux_helpers.shutil, 'which', lambda name: str(path_ccb) if name == 'ccb' else None)

    assert tmux_helpers.detect_ccb_version() == '9.9.9'
