from __future__ import annotations

import os
from pathlib import Path
import subprocess


def test_install_script_links_sidebar_helper() -> None:
    text = Path('install.sh').read_text(encoding='utf-8')

    assert 'bin/ccb-agent-sidebar' in text
    assert 'bin/build-ccb-agent-sidebar' in text
    assert 'build_sidebar_helper_if_possible' in text
    assert 'is_sidebar_wrapper' in text
    assert 'require_sidebar_rust_toolchain' in text
    assert 'cargo build --release --manifest-path "$crate_dir/Cargo.toml"' in text
    assert 'ERROR: ccb-agent-sidebar binary not available' in text


def test_sidebar_bin_wrapper_is_source_install_fallback() -> None:
    text = Path('bin/ccb-agent-sidebar').read_text(encoding='utf-8')

    assert 'CCB_AGENT_SIDEBAR_WRAPPER' in text
    assert 'while [[ -L "$SOURCE" ]]' in text
    assert 'tools/ccb-agent-sidebar/target/release/ccb-agent-sidebar' in text
    assert 'while :; do sleep 3600; done' in text


def test_rust_sidebar_does_not_enable_terminal_mouse_capture() -> None:
    text = Path('tools/ccb-agent-sidebar/src/tui.rs').read_text(encoding='utf-8')

    assert 'EnableMouseCapture' not in text
    assert 'DisableMouseCapture' not in text
    assert 'MouseEventKind' not in text


def test_sidebar_build_script_copies_release_binary() -> None:
    text = Path('bin/build-ccb-agent-sidebar').read_text(encoding='utf-8')

    assert 'cargo build --release --manifest-path "$CRATE_DIR/Cargo.toml"' in text
    assert 'cp -f "$TARGET_BIN" "$OUT_BIN"' in text
    assert 'Built $OUT_BIN' in text


def test_sidebar_package_script_stages_release_artifact() -> None:
    text = Path('bin/package-ccb-agent-sidebar-release').read_text(encoding='utf-8')

    assert 'ARTIFACT_NAME="${CCB_AGENT_SIDEBAR_ARTIFACT_NAME:-ccb-agent-sidebar-linux-x86_64}"' in text
    assert 'STAGE_DIR="$REPO_ROOT/dist/$ARTIFACT_NAME"' in text
    assert 'CCB_AGENT_SIDEBAR_WRAPPER' in text
    assert 'tar -C "$REPO_ROOT/dist" -czf "$OUT_TAR" "$ARTIFACT_NAME"' in text
    assert 'write_sha256_file "$OUT_TAR" "$OUT_SHA"' in text
    assert 'sha256sum "$path" > "$output"' in text
    assert 'shasum -a 256 "$path" > "$output"' in text
    assert 'hashlib.sha256' in text


def test_sidebar_build_script_executes_copy_path_with_fake_cargo(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    bin_dir = repo / 'bin'
    crate_dir = repo / 'tools' / 'ccb-agent-sidebar'
    fake_bin = tmp_path / 'fake-bin'
    bin_dir.mkdir(parents=True)
    crate_dir.mkdir(parents=True)
    fake_bin.mkdir()
    script = bin_dir / 'build-ccb-agent-sidebar'
    script.write_text(Path('bin/build-ccb-agent-sidebar').read_text(encoding='utf-8'), encoding='utf-8')
    script.chmod(0o755)
    (crate_dir / 'Cargo.toml').write_text('[package]\nname = "ccb-agent-sidebar"\nversion = "0.0.0"\n', encoding='utf-8')
    fake_cargo = fake_bin / 'cargo'
    fake_cargo.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
manifest=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--manifest-path" ]]; then
    shift
    manifest="$1"
  fi
  shift || true
done
crate_dir="$(cd "$(dirname "$manifest")" && pwd)"
mkdir -p "$crate_dir/target/release"
cat > "$crate_dir/target/release/ccb-agent-sidebar" <<'BIN'
#!/usr/bin/env bash
echo sidebar-binary
BIN
chmod +x "$crate_dir/target/release/ccb-agent-sidebar"
""",
        encoding='utf-8',
    )
    fake_cargo.chmod(0o755)

    proc = subprocess.run(
        [str(script)],
        cwd=repo,
        env={**os.environ, 'PATH': f'{fake_bin}:{os.environ.get("PATH", "")}'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert 'Built ' in proc.stdout
    out_bin = bin_dir / 'ccb-agent-sidebar'
    assert out_bin.read_text(encoding='utf-8').startswith('#!/usr/bin/env bash')
    assert os.access(out_bin, os.X_OK)


def test_sidebar_package_script_executes_artifact_dry_run(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    bin_dir = repo / 'bin'
    bin_dir.mkdir(parents=True)
    source_bin = bin_dir / 'ccb-agent-sidebar'
    source_bin.write_text('#!/usr/bin/env bash\necho sidebar-binary\n', encoding='utf-8')
    source_bin.chmod(0o755)
    script = bin_dir / 'package-ccb-agent-sidebar-release'
    script.write_text(Path('bin/package-ccb-agent-sidebar-release').read_text(encoding='utf-8'), encoding='utf-8')
    script.chmod(0o755)

    proc = subprocess.run(
        [str(script)],
        cwd=repo,
        env={**os.environ, 'PATH': '/usr/bin:/bin'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert 'Packaged ' in proc.stdout
    artifact = repo / 'dist' / 'ccb-agent-sidebar-linux-x86_64.tar.gz'
    checksum = repo / 'dist' / 'ccb-agent-sidebar-linux-x86_64.tar.gz.sha256'
    assert artifact.is_file()
    assert checksum.is_file()
    checksum_text = checksum.read_text(encoding='utf-8').strip()
    checksum_parts = checksum_text.split()
    assert len(checksum_parts) == 2
    assert len(checksum_parts[0]) == 64
    assert checksum_parts[1].endswith('ccb-agent-sidebar-linux-x86_64.tar.gz')
    listing = subprocess.run(
        ['tar', '-tzf', str(artifact)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert 'ccb-agent-sidebar-linux-x86_64/bin/ccb-agent-sidebar' in listing.stdout


def test_sidebar_package_script_rejects_source_wrapper(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    bin_dir = repo / 'bin'
    bin_dir.mkdir(parents=True)
    source_bin = bin_dir / 'ccb-agent-sidebar'
    source_bin.write_text('#!/usr/bin/env bash\n# CCB_AGENT_SIDEBAR_WRAPPER\n', encoding='utf-8')
    source_bin.chmod(0o755)
    script = bin_dir / 'package-ccb-agent-sidebar-release'
    script.write_text(Path('bin/package-ccb-agent-sidebar-release').read_text(encoding='utf-8'), encoding='utf-8')
    script.chmod(0o755)

    proc = subprocess.run(
        [str(script)],
        cwd=repo,
        env={**os.environ, 'PATH': '/usr/bin:/bin'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert 'source wrapper' in proc.stderr


def test_install_script_prefers_prebuilt_sidebar_binary(tmp_path: Path) -> None:
    install_prefix = tmp_path / 'install'
    crate_dir = install_prefix / 'tools' / 'ccb-agent-sidebar'
    target_bin = crate_dir / 'target' / 'release' / 'ccb-agent-sidebar'
    out_bin = install_prefix / 'bin' / 'ccb-agent-sidebar'
    crate_dir.mkdir(parents=True)
    out_bin.parent.mkdir(parents=True)
    (crate_dir / 'Cargo.toml').write_text('[package]\nname = "ccb-agent-sidebar"\nversion = "0.0.0"\n', encoding='utf-8')
    target_bin.parent.mkdir(parents=True)
    target_bin.write_text('#!/usr/bin/env bash\necho prebuilt-sidebar\n', encoding='utf-8')
    target_bin.chmod(0o755)
    out_bin.write_text('# CCB_AGENT_SIDEBAR_WRAPPER\n', encoding='utf-8')
    out_bin.chmod(0o755)
    harness = tmp_path / 'harness.sh'
    install_text = Path('install.sh').read_text(encoding='utf-8')
    install_body = install_text.rsplit('main "$@"', 1)[0]
    harness.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_INSTALL_PREFIX={install_prefix}
export CODEX_BIN_DIR={tmp_path / 'bin'}
{install_body}
build_sidebar_helper_if_possible
""",
        encoding='utf-8',
    )
    harness.chmod(0o755)

    proc = subprocess.run(
        [str(harness)],
        env={**os.environ, 'PATH': '/usr/bin:/bin'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert 'Installed prebuilt ccb-agent-sidebar' in proc.stdout
    assert out_bin.read_text(encoding='utf-8') == target_bin.read_text(encoding='utf-8')


def test_install_script_rebuilds_sidebar_when_installed_binary_is_not_runnable(tmp_path: Path) -> None:
    install_prefix = tmp_path / 'install'
    crate_dir = install_prefix / 'tools' / 'ccb-agent-sidebar'
    target_bin = crate_dir / 'target' / 'release' / 'ccb-agent-sidebar'
    out_bin = install_prefix / 'bin' / 'ccb-agent-sidebar'
    fake_bin = tmp_path / 'fake-bin'
    crate_dir.mkdir(parents=True)
    target_bin.parent.mkdir(parents=True)
    out_bin.parent.mkdir(parents=True)
    fake_bin.mkdir()
    (crate_dir / 'Cargo.toml').write_text('[package]\nname = "ccb-agent-sidebar"\nversion = "0.0.0"\n', encoding='utf-8')
    target_bin.write_text('#!/usr/bin/env bash\nexit 139\n', encoding='utf-8')
    target_bin.chmod(0o755)
    out_bin.write_text('#!/usr/bin/env bash\nexit 139\n', encoding='utf-8')
    out_bin.chmod(0o755)
    fake_cargo = fake_bin / 'cargo'
    fake_cargo.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
manifest=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--manifest-path" ]]; then
    shift
    manifest="$1"
  fi
  shift || true
done
crate_dir="$(cd "$(dirname "$manifest")" && pwd)"
mkdir -p "$crate_dir/target/release"
cat > "$crate_dir/target/release/ccb-agent-sidebar" <<'BIN'
#!/usr/bin/env bash
if [[ "${1:-}" == "--help" ]]; then
  echo usage >&2
  exit 2
fi
echo rebuilt-sidebar
BIN
chmod +x "$crate_dir/target/release/ccb-agent-sidebar"
""",
        encoding='utf-8',
    )
    fake_cargo.chmod(0o755)
    fake_rustc = fake_bin / 'rustc'
    fake_rustc.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    fake_rustc.chmod(0o755)
    harness = tmp_path / 'harness.sh'
    install_text = Path('install.sh').read_text(encoding='utf-8')
    install_body = install_text.rsplit('main "$@"', 1)[0]
    harness.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_INSTALL_PREFIX={install_prefix}
export CODEX_BIN_DIR={tmp_path / 'bin'}
{install_body}
build_sidebar_helper_if_possible
""",
        encoding='utf-8',
    )
    harness.chmod(0o755)

    proc = subprocess.run(
        [str(harness)],
        env={**os.environ, 'PATH': f'{fake_bin}:/usr/bin:/bin'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert 'Built ccb-agent-sidebar' in proc.stdout
    assert 'rebuilt-sidebar' in out_bin.read_text(encoding='utf-8')


def test_install_script_fails_when_sidebar_build_needs_missing_rust(tmp_path: Path) -> None:
    install_prefix = tmp_path / 'install'
    crate_dir = install_prefix / 'tools' / 'ccb-agent-sidebar'
    out_bin = install_prefix / 'bin' / 'ccb-agent-sidebar'
    fake_bin = tmp_path / 'fake-bin'
    crate_dir.mkdir(parents=True)
    out_bin.parent.mkdir(parents=True)
    fake_bin.mkdir()
    (crate_dir / 'Cargo.toml').write_text('[package]\nname = "ccb-agent-sidebar"\nversion = "0.0.0"\n', encoding='utf-8')
    out_bin.write_text('# CCB_AGENT_SIDEBAR_WRAPPER\n', encoding='utf-8')
    out_bin.chmod(0o755)
    harness = tmp_path / 'harness.sh'
    install_text = Path('install.sh').read_text(encoding='utf-8')
    install_body = install_text.rsplit('main "$@"', 1)[0]
    harness.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_INSTALL_PREFIX={install_prefix}
export CODEX_BIN_DIR={tmp_path / 'bin'}
{install_body}
build_sidebar_helper_if_possible
""",
        encoding='utf-8',
    )
    harness.chmod(0o755)

    proc = subprocess.run(
        ['/bin/bash', str(harness)],
        env={**os.environ, 'PATH': '/usr/bin:/bin'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert 'ERROR: Rust toolchain required to build ccb-agent-sidebar' in proc.stdout
    assert 'Missing:' in proc.stdout
    assert 'cargo' in proc.stdout
    assert 'rustc' in proc.stdout


def test_install_copy_excludes_rust_target_directory() -> None:
    text = Path('install.sh').read_text(encoding='utf-8')

    assert "--exclude 'target/'" in text
    assert "--exclude 'target'" in text


def test_ci_runs_rust_sidebar_checks() -> None:
    text = Path('.github/workflows/test.yml').read_text(encoding='utf-8')

    assert 'name: Rust sidebar' in text
    assert 'cargo test --manifest-path tools/ccb-agent-sidebar/Cargo.toml' in text
    assert 'bin/build-ccb-agent-sidebar' in text


def test_macos_install_smoke_uses_prebuilt_sidebar_helper() -> None:
    text = Path('.github/workflows/test.yml').read_text(encoding='utf-8')

    build_marker = 'Build macOS sidebar helper for release install smoke'
    smoke_marker = 'Smoke macOS release install'
    assert build_marker in text
    assert text.index(build_marker) < text.index(smoke_marker)
    assert 'run: bin/build-ccb-agent-sidebar' in text


def test_sidebar_release_workflow_publishes_linux_artifact() -> None:
    text = Path('.github/workflows/release-sidebar.yml').read_text(encoding='utf-8')

    assert 'name: Release Sidebar Helper' in text
    assert 'runs-on: ubuntu-22.04' in text
    assert 'workflow_dispatch:' in text
    assert 'tags:' in text
    assert 'cargo test --manifest-path tools/ccb-agent-sidebar/Cargo.toml' in text
    assert 'bin/build-ccb-agent-sidebar' in text
    assert 'bin/package-ccb-agent-sidebar-release' in text
    assert 'ccb-agent-sidebar-linux-x86_64.tar.gz' in text
    assert 'actions/upload-artifact@v4' in text
    assert 'softprops/action-gh-release@v2' in text


def test_release_artifacts_workflow_sets_up_rust_for_sidebar_build() -> None:
    text = Path('.github/workflows/release-artifacts.yml').read_text(encoding='utf-8')

    assert 'default: "v7.0.7"' in text
    assert 'os: ubuntu-22.04' in text
    assert 'uses: dtolnay/rust-toolchain@stable' in text
    assert 'rustup target add x86_64-apple-darwin aarch64-apple-darwin' in text
    assert "grep -F 'universal binary'" in text
    assert '"$helper" --help' in text
