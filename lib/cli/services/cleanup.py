from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import time

from cli.services.daemon import inspect_daemon
from provider_execution.state_store import ExecutionStateStore
from storage.locks import file_lock


_PENDING_JOB_STATUSES = {'accepted', 'queued', 'running'}
_SAFE_GEMINI_CACHE_RELS = (
    Path('.npm') / '_cacache',
    Path('.cache') / 'node-gyp',
    Path('.cache') / 'vscode-ripgrep',
)
_SAFE_CLAUDE_CACHE_RELS = (
    Path('.cache') / 'claude',
    Path('.npm') / '_logs',
    Path('.claude') / 'cache',
    Path('.claude') / 'telemetry',
    Path('.claude') / 'paste-cache',
    Path('.claude') / 'plugins' / 'marketplaces',
)
_GEMINI_SHARED_CACHE_RELS = (
    Path('npm') / '_cacache',
    Path('xdg') / 'node-gyp',
    Path('xdg') / 'vscode-ripgrep',
)
_PANE_CRASH_LOG_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_PANE_CRASH_LOG_MAX_KEEP_PER_RUNTIME = 50


@dataclass(frozen=True)
class CleanupAction:
    provider: str
    kind: str
    path: str
    bytes_removed: int
    reason: str


@dataclass(frozen=True)
class CleanupSkipped:
    provider: str
    path: str
    reason: str


@dataclass(frozen=True)
class CleanupSummary:
    project_root: str
    project_id: str
    status: str
    deleted_bytes: int
    deleted_count: int
    skipped_count: int
    actions: tuple[CleanupAction, ...] = ()
    skipped: tuple[CleanupSkipped, ...] = ()


def cleanup_project_storage(context, command) -> CleanupSummary:
    del command
    with file_lock(context.paths.ccbd_dir / 'startup.lock'):
        _require_stopped_backend(context)
        _require_no_pending_jobs(context)
        actions: list[CleanupAction] = []
        skipped: list[CleanupSkipped] = []
        _cleanup_claude_version_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_claude_rebuildable_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_gemini_rebuildable_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_pane_crash_logs(context.paths, actions=actions, skipped=skipped)
        return CleanupSummary(
            project_root=str(context.project.project_root),
            project_id=context.project.project_id,
            status='ok',
            deleted_bytes=sum(item.bytes_removed for item in actions),
            deleted_count=len(actions),
            skipped_count=len(skipped),
            actions=tuple(actions),
            skipped=tuple(skipped),
        )


def _require_stopped_backend(context) -> None:
    _manager, _guard, inspection = inspect_daemon(context)
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    desired_state = str(getattr(inspection, 'desired_state', '') or '').strip()
    if getattr(inspection, 'pid_alive', False) or getattr(inspection, 'socket_connectable', False):
        raise RuntimeError('ccb cleanup requires stopped ccbd; run `ccb kill` first')
    if phase not in {'', 'unmounted', 'failed'}:
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; current phase={phase}')
    if desired_state and desired_state != 'stopped':
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; desired_state={desired_state}')


def _require_no_pending_jobs(context) -> None:
    execution_summary = ExecutionStateStore(context.paths).summary()
    active_execution_count = int(execution_summary.get('active_execution_count') or 0)
    pending_items_count = int(execution_summary.get('pending_items_count') or 0)
    terminal_pending_count = int(execution_summary.get('terminal_pending_count') or 0)
    pending_job_count = _pending_job_count(context.paths)
    if active_execution_count or pending_items_count or terminal_pending_count or pending_job_count:
        raise RuntimeError(
            'ccb cleanup refused: pending ask jobs exist; wait for completion or run `ccb kill` after terminalization'
        )


def _pending_job_count(layout) -> int:
    roots = [layout.agents_dir, layout.ccbd_dir / 'targets']
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob('jobs.jsonl')):
            count += _pending_job_count_in_file(path)
    return count


def _pending_job_count_in_file(path: Path) -> int:
    latest_by_job: dict[str, str] = {}
    unreadable_or_malformed_count = 0
    try:
        handle = path.open('r', encoding='utf-8')
    except OSError:
        return 1
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                unreadable_or_malformed_count += 1
                continue
            if not isinstance(record, dict):
                unreadable_or_malformed_count += 1
                continue
            job_id = str(record.get('job_id') or '').strip()
            if not job_id:
                continue
            latest_by_job[job_id] = str(record.get('status') or '').strip().lower()
    return (
        sum(1 for status in latest_by_job.values() if status in _PENDING_JOB_STATUSES)
        + unreadable_or_malformed_count
    )


def _cleanup_claude_version_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    legacy_shared_versions = layout.shared_cache_dir / 'claude' / 'versions'
    external_versions = layout.provider_external_cache_dir('claude') / 'versions'
    legacy_shared_active_names: set[str] = set()
    external_active_names: set[str] = set()
    if agents_dir.exists():
        for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
            active_name = _current_claude_version_name(home)
            versions_dir = home / '.local' / 'share' / 'claude' / 'versions'
            if active_name and versions_dir.is_symlink():
                if _same_path(versions_dir, legacy_shared_versions):
                    legacy_shared_active_names.add(active_name)
                if _same_path(versions_dir, external_versions):
                    external_active_names.add(active_name)
            _cleanup_one_claude_versions_dir(
                versions_dir,
                active_version_names={active_name} if active_name else set(),
                actions=actions,
                skipped=skipped,
            )
    _cleanup_shared_claude_versions_dir(
        legacy_shared_versions,
        active_version_names=legacy_shared_active_names,
        actions=actions,
        skipped=skipped,
    )
    _cleanup_shared_claude_versions_dir(
        external_versions,
        active_version_names=external_active_names,
        actions=actions,
        skipped=skipped,
    )


def _cleanup_one_claude_versions_dir(
    versions_dir: Path,
    *,
    active_version_names: set[str],
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if versions_dir.is_symlink():
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='versions_dir_is_symlink',
            )
        )
        return
    if not versions_dir.is_dir():
        return
    version_paths = _claude_version_paths(versions_dir)
    if not version_paths:
        return
    if not active_version_names:
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='current_version_symlink_unresolved',
            )
        )
        return
    _prune_claude_versions(
        versions_dir,
        version_paths,
        active_version_names=active_version_names,
        provider='claude',
        reason='old_claude_version_cache',
        actions=actions,
        skipped=skipped,
    )


def _cleanup_shared_claude_versions_dir(
    versions_dir: Path,
    *,
    active_version_names: set[str],
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if not versions_dir.exists():
        return
    if versions_dir.is_symlink():
        skipped.append(CleanupSkipped(provider='claude', path=str(versions_dir), reason='shared_versions_dir_is_symlink'))
        return
    if not versions_dir.is_dir():
        return
    version_paths = _claude_version_paths(versions_dir)
    if not version_paths:
        return
    _prune_claude_versions(
        versions_dir,
        version_paths,
        active_version_names=active_version_names,
        provider='claude',
        reason='old_shared_claude_version_cache' if active_version_names else 'unreferenced_shared_claude_version_cache',
        actions=actions,
        skipped=skipped,
    )


def _prune_claude_versions(
    versions_dir: Path,
    version_paths: list[Path],
    *,
    active_version_names: set[str],
    provider: str,
    reason: str,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    keep = {path for path in version_paths if path.name in active_version_names}
    for path in version_paths:
        if path in keep:
            continue
        _remove_tree(
            path,
            root=versions_dir,
            provider=provider,
            kind='version_cache',
            reason=reason,
            actions=actions,
            skipped=skipped,
        )


def _current_claude_version_name(home: Path) -> str | None:
    link = home / '.local' / 'bin' / 'claude'
    try:
        target = link.resolve(strict=True)
    except Exception:
        return None
    versions_dir = home / '.local' / 'share' / 'claude' / 'versions'
    if not _is_within(target, versions_dir):
        return None
    try:
        relative = target.relative_to(versions_dir.resolve(strict=False))
    except Exception:
        return None
    if not relative.parts:
        return None
    return relative.parts[0]


def _claude_version_paths(versions_dir: Path) -> list[Path]:
    try:
        entries = sorted(versions_dir.iterdir(), key=lambda path: (_version_key(path.name), _safe_mtime(path), path.name))
    except OSError:
        return []
    return [
        path
        for path in entries
        if _looks_like_claude_version_name(path.name)
        and (path.is_file() or path.is_dir())
        and not path.is_symlink()
        and _is_within(path, versions_dir)
    ]


def _looks_like_claude_version_name(value: str) -> bool:
    if not value or not value[0].isdigit():
        return False
    return all(item.isalnum() or item in {'.', '_', '-'} for item in value)


def _newest_version_path(paths) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_version_key(path.name), _safe_mtime(path), path.name))


def _version_key(value: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for item in value.replace('-', '.').split('.'):
        if item.isdigit():
            parts.append((1, int(item)))
        else:
            parts.append((0, item))
    return tuple(parts)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _same_path(left: Path, right: Path) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except Exception:
        return False


def _cleanup_claude_rebuildable_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
        if not home.is_dir() or home.is_symlink():
            continue
        for relative in _SAFE_CLAUDE_CACHE_RELS:
            path = home / relative
            if not path.exists():
                continue
            _remove_tree(
                path,
                root=home,
                provider='claude',
                kind='tool_cache',
                reason='rebuildable_claude_cache',
                actions=actions,
                skipped=skipped,
            )


def _cleanup_gemini_rebuildable_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if agents_dir.exists():
        for home in sorted(agents_dir.glob('*/provider-state/gemini/home')):
            if not home.is_dir() or home.is_symlink():
                continue
            for relative in _SAFE_GEMINI_CACHE_RELS:
                path = home / relative
                if not path.exists():
                    continue
                _remove_tree(
                    path,
                    root=home,
                    provider='gemini',
                    kind='tool_cache',
                    reason='rebuildable_gemini_cache',
                    actions=actions,
                    skipped=skipped,
                )
    _cleanup_gemini_cache_root(
        layout.shared_cache_dir / 'gemini',
        safe_rels=_GEMINI_SHARED_CACHE_RELS,
        actions=actions,
        skipped=skipped,
    )
    _cleanup_gemini_cache_root(
        layout.provider_external_cache_dir('gemini'),
        safe_rels=_GEMINI_SHARED_CACHE_RELS,
        actions=actions,
        skipped=skipped,
    )


def _cleanup_gemini_cache_root(
    cache_root: Path,
    *,
    safe_rels: tuple[Path, ...],
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if not cache_root.exists() or cache_root.is_symlink():
        return
    for relative in safe_rels:
        path = cache_root / relative
        if not path.exists():
            continue
        _remove_tree(
            path,
            root=cache_root,
            provider='gemini',
            kind='tool_cache',
            reason='rebuildable_gemini_cache',
            actions=actions,
            skipped=skipped,
        )


def _cleanup_pane_crash_logs(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    now = time.time()
    for runtime_dir in sorted(agents_dir.glob('*/provider-runtime/*')):
        if not runtime_dir.is_dir() or runtime_dir.is_symlink():
            continue
        logs = sorted(
            (path for path in runtime_dir.glob('pane-crash-*.log') if path.is_file() and not path.is_symlink()),
            key=lambda path: (_safe_mtime(path), path.name),
            reverse=True,
        )
        for index, path in enumerate(logs):
            age = now - _safe_mtime(path)
            if index < _PANE_CRASH_LOG_MAX_KEEP_PER_RUNTIME and age < _PANE_CRASH_LOG_MAX_AGE_SECONDS:
                continue
            _remove_tree(
                path,
                root=runtime_dir,
                provider=runtime_dir.name,
                kind='crash_log',
                reason='old_pane_crash_log',
                actions=actions,
                skipped=skipped,
            )


def _remove_tree(
    path: Path,
    *,
    root: Path,
    provider: str,
    kind: str,
    reason: str,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if path.is_symlink():
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='symlink_not_removed'))
        return
    if not _is_within(path, root):
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='path_out_of_bounds'))
        return
    size = _tree_size(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    actions.append(
        CleanupAction(
            provider=provider,
            kind=kind,
            path=str(path),
            bytes_removed=size,
            reason=reason,
        )
    )


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return _lstat_size(path)
    for child in path.rglob('*'):
        total += _lstat_size(child)
    return total


def _lstat_size(path: Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


__all__ = ['CleanupAction', 'CleanupSkipped', 'CleanupSummary', 'cleanup_project_storage']
