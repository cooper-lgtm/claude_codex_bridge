from __future__ import annotations

from pathlib import Path
import shutil

from provider_core.projected_assets import tree_content_fingerprint, write_projected_marker

_PROJECTION_LABEL = 'claude-binary-versions'
_IGNORED_VERSION_ENTRIES = {'.DS_Store'}


def route_claude_binary_cache(home_root: Path, shared_cache_root: Path) -> dict[str, object]:
    home = Path(home_root).expanduser().resolve(strict=False)
    shared_versions_dir = Path(shared_cache_root).expanduser().resolve(strict=False) / 'versions'
    versions_dir = home / '.local' / 'share' / 'claude' / 'versions'

    try:
        shared_versions_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _result(
            status='skipped',
            reason='shared_cache_unavailable',
            versions_dir=versions_dir,
            shared_versions_dir=shared_versions_dir,
            error_detail=str(exc),
        )

    if versions_dir.is_symlink():
        if _same_path(versions_dir, shared_versions_dir):
            active_version_name = _ensure_latest_claude_link(home, shared_versions_dir)
            write_projected_marker(
                versions_dir,
                label=_PROJECTION_LABEL,
                mode='symlink',
                source=shared_versions_dir,
            )
            return _result(
                status='ok',
                reason='already_shared',
                versions_dir=versions_dir,
                shared_versions_dir=shared_versions_dir,
                version_names=_version_names(shared_versions_dir),
                active_version_name=active_version_name,
            )
        try:
            source_versions_dir = versions_dir.resolve(strict=True)
        except Exception as exc:
            return _result(
                status='skipped',
                reason='versions_symlink_target_unavailable',
                versions_dir=versions_dir,
                shared_versions_dir=shared_versions_dir,
                error_detail=str(exc),
            )
        if not source_versions_dir.is_dir():
            return _result(
                status='skipped',
                reason='versions_dir_symlink_not_shared',
                versions_dir=versions_dir,
                shared_versions_dir=shared_versions_dir,
            )
        scan = _scan_versions_dir(source_versions_dir)
        if scan['unknown_entries']:
            return _result(
                status='skipped',
                reason='versions_dir_symlink_not_shared',
                versions_dir=versions_dir,
                shared_versions_dir=shared_versions_dir,
                version_names=scan['version_names'],
                warnings=scan['unknown_entries'],
            )
        failure = _copy_versions_to_shared(
            version_paths=scan['version_paths'],
            shared_versions_dir=shared_versions_dir,
            versions_dir=versions_dir,
            version_names=scan['version_names'],
        )
        if failure is not None:
            return failure
        linked = _link_versions_dir(
            versions_dir,
            shared_versions_dir,
            reason='migrated_symlink' if scan['version_paths'] else 'linked_empty',
            version_names=scan['version_names'],
        )
        if linked.get('status') == 'ok':
            linked['active_version_name'] = _ensure_latest_claude_link(home, shared_versions_dir) or ''
        if scan['ignored_entries'] and linked.get('status') == 'ok':
            linked['warnings'] = tuple(scan['ignored_entries'])
        return linked

    if versions_dir.exists() and not versions_dir.is_dir():
        return _result(
            status='skipped',
            reason='versions_path_not_directory',
            versions_dir=versions_dir,
            shared_versions_dir=shared_versions_dir,
        )

    if not versions_dir.exists():
        return _link_versions_dir(
            versions_dir,
            shared_versions_dir,
            reason='linked_empty',
        )

    scan = _scan_versions_dir(versions_dir)
    if scan['unknown_entries']:
        return _result(
            status='skipped',
            reason='unknown_versions_entries',
            versions_dir=versions_dir,
            shared_versions_dir=shared_versions_dir,
            version_names=scan['version_names'],
            warnings=scan['unknown_entries'],
        )

    failure = _copy_versions_to_shared(
        version_paths=scan['version_paths'],
        shared_versions_dir=shared_versions_dir,
        versions_dir=versions_dir,
        version_names=scan['version_names'],
    )
    if failure is not None:
        return failure

    linked = _link_versions_dir(
        versions_dir,
        shared_versions_dir,
        reason='migrated' if scan['version_paths'] else 'linked_empty',
        version_names=scan['version_names'],
    )
    if linked.get('status') == 'ok':
        linked['active_version_name'] = _ensure_latest_claude_link(home, shared_versions_dir) or ''
    if scan['ignored_entries'] and linked.get('status') == 'ok':
        linked['warnings'] = tuple(scan['ignored_entries'])
    return linked


def _copy_versions_to_shared(
    *,
    version_paths: tuple[Path, ...],
    shared_versions_dir: Path,
    versions_dir: Path,
    version_names: tuple[str, ...],
) -> dict[str, object] | None:
    for version_path in version_paths:
        destination = shared_versions_dir / version_path.name
        if destination.exists() and _version_fingerprint(destination) != _version_fingerprint(version_path):
            return _result(
                status='skipped',
                reason='shared_version_content_conflict',
                versions_dir=versions_dir,
                shared_versions_dir=shared_versions_dir,
                version_names=version_names,
                warnings=(version_path.name,),
            )
        if not destination.exists():
            try:
                _copy_version_atomic(version_path, destination)
            except Exception as exc:
                return _result(
                    status='skipped',
                    reason='shared_version_copy_failed',
                    versions_dir=versions_dir,
                    shared_versions_dir=shared_versions_dir,
                    version_names=version_names,
                    error_detail=str(exc),
                )
        write_projected_marker(destination, label='claude-binary-version', mode='copy', source=version_path)
    return None


def _scan_versions_dir(versions_dir: Path) -> dict[str, object]:
    version_paths: list[Path] = []
    unknown_entries: list[str] = []
    ignored_entries: list[str] = []
    try:
        entries = sorted(versions_dir.iterdir(), key=lambda item: item.name)
    except Exception:
        return {'version_paths': (), 'version_names': (), 'unknown_entries': ('unreadable_versions_dir',), 'ignored_entries': ()}
    for entry in entries:
        if entry.name in _IGNORED_VERSION_ENTRIES or entry.name.endswith('.ccb-projection.json'):
            ignored_entries.append(entry.name)
            continue
        if not _looks_like_claude_version_name(entry.name):
            unknown_entries.append(entry.name)
            continue
        if entry.is_dir() and not entry.is_symlink() and (entry / 'claude').is_file():
            version_paths.append(entry)
            continue
        if entry.is_file() and not entry.is_symlink():
            version_paths.append(entry)
            continue
        unknown_entries.append(entry.name)
    return {
        'version_paths': tuple(version_paths),
        'version_names': tuple(path.name for path in version_paths),
        'unknown_entries': tuple(unknown_entries),
        'ignored_entries': tuple(ignored_entries),
    }


def _link_versions_dir(
    versions_dir: Path,
    shared_versions_dir: Path,
    *,
    reason: str,
    version_names: tuple[str, ...] = (),
) -> dict[str, object]:
    try:
        versions_dir.parent.mkdir(parents=True, exist_ok=True)
        if versions_dir.exists() or versions_dir.is_symlink():
            _remove_path(versions_dir)
        versions_dir.symlink_to(shared_versions_dir, target_is_directory=True)
        write_projected_marker(
            versions_dir,
            label=_PROJECTION_LABEL,
            mode='symlink',
            source=shared_versions_dir,
        )
    except Exception as exc:
        return _result(
            status='skipped',
            reason='versions_link_failed',
            versions_dir=versions_dir,
            shared_versions_dir=shared_versions_dir,
            version_names=version_names,
            error_detail=str(exc),
        )
    return _result(
        status='ok',
        reason=reason,
        versions_dir=versions_dir,
        shared_versions_dir=shared_versions_dir,
        version_names=version_names or _version_names(shared_versions_dir),
    )


def _ensure_latest_claude_link(home: Path, shared_versions_dir: Path) -> str:
    latest = _newest_version_path(shared_versions_dir)
    if latest is None:
        return ''
    executable = _version_executable_path(latest)
    if executable is None:
        return ''
    link = home / '.local' / 'bin' / 'claude'
    try:
        if link.is_symlink() and _same_path(link, executable):
            return latest.name
        if link.exists() and not link.is_symlink():
            return ''
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(executable)
    except Exception:
        return ''
    return latest.name


def _newest_version_path(versions_dir: Path) -> Path | None:
    try:
        candidates = [
            child
            for child in versions_dir.iterdir()
            if _looks_like_claude_version_name(child.name)
            and not child.name.endswith('.ccb-projection.json')
            and not child.is_symlink()
            and _version_executable_path(child) is not None
        ]
    except Exception:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_version_key(path.name), path.stat().st_mtime, path.name))


def _version_executable_path(version_path: Path) -> Path | None:
    if version_path.is_file():
        return version_path
    executable = version_path / 'claude'
    if executable.is_file():
        return executable
    return None


def _version_names(versions_dir: Path) -> tuple[str, ...]:
    try:
        return tuple(
            sorted(
                child.name
                for child in versions_dir.iterdir()
                if not child.name.endswith('.ccb-projection.json')
                if _looks_like_claude_version_name(child.name)
                and not child.is_symlink()
                and (child.is_file() or child.is_dir())
            )
        )
    except Exception:
        return ()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return False


def _copy_version_atomic(source: Path, destination: Path) -> None:
    if source.is_file():
        _copyfile_atomic(source, destination)
        return
    _copytree_atomic(source, destination)


def _copyfile_atomic(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f'.{destination.name}.tmp')
    _remove_path(tmp)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, tmp, follow_symlinks=False)
        tmp.rename(destination)
    except Exception:
        _remove_path(tmp)
        raise


def _copytree_atomic(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f'.{destination.name}.tmp')
    _remove_path(tmp)
    try:
        shutil.copytree(source, tmp, symlinks=True)
        tmp.rename(destination)
    except Exception:
        _remove_path(tmp)
        raise


def _version_fingerprint(path: Path) -> str:
    if path.is_file():
        return _file_content_fingerprint(path)
    return tree_content_fingerprint(path)


def _file_content_fingerprint(path: Path) -> str:
    from hashlib import sha256

    digest = sha256()
    try:
        with Path(path).open('rb') as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b''):
                digest.update(chunk)
    except Exception:
        return ''
    return digest.hexdigest()


def _looks_like_claude_version_name(value: str) -> bool:
    if not value or not value[0].isdigit():
        return False
    return all(item.isalnum() or item in {'.', '_', '-'} for item in value)


def _version_key(value: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for item in value.replace('-', '.').split('.'):
        if item.isdigit():
            parts.append((1, int(item)))
        else:
            parts.append((0, item))
    return tuple(parts)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def _result(
    *,
    status: str,
    reason: str,
    versions_dir: Path,
    shared_versions_dir: Path,
    version_names: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    error_detail: str = '',
    active_version_name: str = '',
) -> dict[str, object]:
    return {
        'status': status,
        'reason': reason,
        'versions_dir': str(versions_dir),
        'shared_versions_dir': str(shared_versions_dir),
        'version_names': tuple(version_names),
        'warnings': tuple(warnings),
        'error_detail': error_detail,
        'active_version_name': active_version_name,
    }


__all__ = ['route_claude_binary_cache']
