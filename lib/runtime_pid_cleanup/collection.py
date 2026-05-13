from __future__ import annotations

import json
import os
from pathlib import Path

from provider_runtime.helper_manifest import load_helper_manifest
from storage.paths import PathLayout

from .procfs import read_pid_file, read_proc_cmdline
from .utils import coerce_pid, resolved_runtime_roots


def collect_pid_candidates(agent_dir: Path, *, runtime, fallback_to_agent_dir: bool) -> dict[int, list[Path]]:
    candidates: dict[int, list[Path]] = {}
    if runtime is not None:
        runtime_pid = coerce_pid(getattr(runtime, 'runtime_pid', None) or getattr(runtime, 'pid', None))
        if runtime_pid is not None:
            candidates.setdefault(runtime_pid, []).append(agent_dir / 'runtime.json')
    for root in resolved_runtime_roots(agent_dir, runtime=runtime, fallback_to_agent_dir=fallback_to_agent_dir):
        for pid_path in sorted(root.rglob('*.pid')):
            pid = read_pid_file(pid_path)
            if pid is None:
                continue
            candidates.setdefault(pid, []).append(pid_path)
    helper_path = agent_dir / 'helper.json'
    helper = _load_helper_manifest_best_effort(helper_path)
    if helper is not None:
        candidates.setdefault(helper.leader_pid, []).append(helper_path)
    return candidates


def collect_project_process_candidates(
    project_root: Path,
    *,
    proc_root: Path = Path('/proc'),
    read_proc_cmdline_fn=read_proc_cmdline,
    current_pid: int | None = None,
) -> dict[int, list[Path]]:
    current_pid = int(current_pid or os.getpid())
    ccb_root = project_root.expanduser() / '.ccb'
    markers = _project_runtime_markers(project_root, ccb_root=ccb_root)
    if not markers:
        return {}
    candidates: dict[int, list[Path]] = {}
    try:
        entries = list(proc_root.iterdir())
    except Exception:
        return candidates
    for entry in entries:
        pid = coerce_pid(entry.name)
        if pid is None or pid == current_pid:
            continue
        cmdline = str(read_proc_cmdline_fn(pid) or '').strip()
        matched_markers = tuple(marker for marker in markers if str(marker) in cmdline)
        if not matched_markers:
            continue
        candidates.setdefault(pid, []).extend(matched_markers)
    return candidates


__all__ = ['collect_pid_candidates', 'collect_project_process_candidates']


def _load_helper_manifest_best_effort(path: Path):
    try:
        return load_helper_manifest(path)
    except Exception:
        return None


def _project_runtime_markers(project_root: Path, *, ccb_root: Path) -> tuple[Path, ...]:
    layout = PathLayout(project_root)
    markers: list[Path] = [ccb_root]
    if layout.runtime_state_placement.root_kind == 'relocated':
        for path in (layout.runtime_state_root / 'agents', layout.runtime_state_root / 'ccbd'):
            if path not in markers:
                markers.append(path)
    return tuple(markers)


def collect_project_authority_pid_candidates(project_root: Path) -> dict[int, list[Path]]:
    layout = PathLayout(project_root)
    candidates: dict[int, list[Path]] = {}
    for path, keys in (
        (layout.ccbd_lease_path, ('ccbd_pid', 'keeper_pid')),
        (layout.ccbd_keeper_path, ('keeper_pid',)),
    ):
        payload = _load_json_object(path)
        if payload is None:
            continue
        for key in keys:
            pid = coerce_pid(payload.get(key))
            if pid is None:
                continue
            candidates.setdefault(pid, []).append(path)
    return candidates


def _load_json_object(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
