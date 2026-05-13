from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

_HASH_CHUNK_SIZE = 64 * 1024


def route_projected_tree(
    source: Path,
    target: Path,
    *,
    enabled: bool = True,
    label: str = 'projected-tree',
    marker_path: Path | None = None,
    allow_unmarked_replace: bool = False,
) -> bool:
    source = Path(source).expanduser()
    target = Path(target).expanduser()
    marker = marker_path or _default_marker_path(target)

    if not enabled or not source.is_dir():
        _remove_projected_target(target, marker, allow_unmarked_replace=allow_unmarked_replace)
        return False
    if _same_path(source, target):
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    if _projection_points_to(target, source):
        _write_projection_marker(marker, source=source, mode='symlink', label=label)
        return True
    if target.exists() or target.is_symlink():
        if not _can_replace_projected_target(
            target,
            marker,
            allow_unmarked_replace=allow_unmarked_replace,
            replacement_source=source,
        ):
            return False
        _remove_path(target)
    try:
        target.symlink_to(source, target_is_directory=True)
        _write_projection_marker(marker, source=source, mode='symlink', label=label)
        return True
    except Exception:
        _remove_path(target)
    try:
        shutil.copytree(source, target)
        _write_projection_marker(marker, source=source, mode='copy', label=label)
        return True
    except Exception:
        _remove_path(target)
    return False


def copy_projected_tree_to_cache(source: Path, bundle_root: Path, *, label: str = 'projected-tree') -> bool:
    source = Path(source).expanduser()
    bundle_root = Path(bundle_root).expanduser()
    if not source.is_dir():
        return False
    if _tree_has_required_entries(source, bundle_root):
        write_projected_marker(bundle_root, label=label, mode='copy', source=source)
        return True
    tmp_root = bundle_root.with_name(f'.{bundle_root.name}.tmp')
    _remove_path(tmp_root)
    tmp_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, tmp_root)
        _remove_path(bundle_root)
        tmp_root.rename(bundle_root)
        write_projected_marker(bundle_root, label=label, mode='copy', source=source)
    except Exception:
        _remove_path(tmp_root)
        return False
    return True


def ensure_shared_tree_bundle(source: Path, bundle_root: Path) -> Path | None:
    return bundle_root if copy_projected_tree_to_cache(source, bundle_root) else None


def remove_projected_path(
    target: Path,
    *,
    label: str = 'projected-tree',
    source: Path | None = None,
    marker_path: Path | None = None,
    allow_unmarked_replace: bool = False,
) -> None:
    target = Path(target).expanduser()
    marker = marker_path or _default_marker_path(target)
    if not _marker_matches(marker, label=label, source=source):
        if allow_unmarked_replace and target.is_symlink():
            _remove_path(target)
        return
    _remove_projected_target(target, marker, allow_unmarked_replace=allow_unmarked_replace)


def remove_projected_tree(
    target: Path,
    *,
    marker_path: Path | None = None,
    allow_unmarked_replace: bool = False,
) -> None:
    remove_projected_path(target, marker_path=marker_path, allow_unmarked_replace=allow_unmarked_replace)


def write_projected_marker(target: Path, *, label: str, mode: str, source: Path) -> None:
    _write_projection_marker(_default_marker_path(Path(target).expanduser()), source=source, mode=mode, label=label)


def tree_content_fingerprint(root: Path) -> str:
    root = Path(root).expanduser()
    digest = hashlib.sha256()
    try:
        for entry in sorted(root.rglob('*')):
            relative = entry.relative_to(root)
            kind = 'd' if entry.is_dir() else 'f' if entry.is_file() else 'l' if entry.is_symlink() else 'o'
            digest.update(kind.encode('utf-8'))
            digest.update(b'\0')
            digest.update(str(relative).encode('utf-8', errors='ignore'))
            digest.update(b'\0')
            if entry.is_file():
                with entry.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b''):
                        digest.update(chunk)
            elif entry.is_symlink():
                digest.update(str(entry.readlink()).encode('utf-8', errors='ignore'))
            digest.update(b'\0')
    except Exception:
        return ''
    return digest.hexdigest()


def _remove_projected_target(target: Path, marker: Path, *, allow_unmarked_replace: bool) -> None:
    if _can_replace_projected_target(target, marker, allow_unmarked_replace=allow_unmarked_replace):
        _remove_path(target)
        marker.unlink(missing_ok=True)


def _projection_points_to(target: Path, source: Path) -> bool:
    if not target.is_symlink():
        return False
    try:
        return target.resolve() == source.resolve()
    except Exception:
        try:
            return target.readlink() == source
        except Exception:
            return False


def _can_replace_projected_target(
    target: Path,
    marker: Path,
    *,
    allow_unmarked_replace: bool,
    replacement_source: Path | None = None,
) -> bool:
    if marker.is_file():
        return True
    if not target.exists():
        return True
    if target.is_symlink():
        return allow_unmarked_replace
    if allow_unmarked_replace:
        return True
    if replacement_source is not None and target.is_dir() and replacement_source.is_dir():
        return tree_content_fingerprint(target) == tree_content_fingerprint(replacement_source)
    return allow_unmarked_replace


def _tree_has_required_entries(source: Path, candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    try:
        for entry in source.rglob('*'):
            relative = entry.relative_to(source)
            projected = candidate / relative
            if entry.is_dir() and not projected.is_dir():
                return False
            if entry.is_file() and not projected.is_file():
                return False
            if entry.is_symlink() and not projected.exists() and not projected.is_symlink():
                return False
    except Exception:
        return False
    return True


def _default_marker_path(target: Path) -> Path:
    return Path(f'{target}.ccb-projection.json')


def _marker_matches(marker: Path, *, label: str, source: Path | None) -> bool:
    try:
        payload = json.loads(marker.read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get('record_type') != 'ccb_projected_asset':
        return False
    if str(payload.get('label') or '') != label:
        return False
    if source is None:
        return True
    try:
        return Path(str(payload.get('source') or '')).expanduser().resolve() == Path(source).expanduser().resolve()
    except Exception:
        return str(payload.get('source') or '') == str(source)


def _write_projection_marker(path: Path, *, source: Path, mode: str, label: str) -> None:
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_projected_asset',
        'label': label,
        'source': str(source),
        'mode': mode,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + '\n', encoding='utf-8')
    except Exception:
        pass


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return left == right


__all__ = [
    'copy_projected_tree_to_cache',
    'ensure_shared_tree_bundle',
    'remove_projected_path',
    'remove_projected_tree',
    'route_projected_tree',
    'tree_content_fingerprint',
    'write_projected_marker',
]
