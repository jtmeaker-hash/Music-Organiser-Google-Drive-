"""
Stage 1 tests: installer distro/PRoot detection, root pkg guard, config preservation,
missing import failure, and Termux wake-lock detection vs acquisition reporting.
"""
import os
import stat
import subprocess
import sys
from pathlib import Path
import pytest

import musicvault
from musicvault import termux


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_version_bumped_to_2_0_1():
    assert musicvault.__version__ == "2.0.1"
    assert sys.version_info >= (3, 11)


def test_installer_selects_apt_in_debian_proot_even_when_pkg_present(tmp_path: Path):
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "pkg", "#!/usr/bin/env bash\necho 'Cannot run pkg command as root' >&2\nexit 1\n")
    _make_executable(bin_dir / "apt-get", "#!/usr/bin/env bash\nexit 0\n")

    os_release = tmp_path / "os-release"
    os_release.write_text(
        'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MUSICVAULT_OS_RELEASE"] = str(os_release)
    env["MUSICVAULT_EFFECTIVE_UID"] = "0"

    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--detect-only"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SELECTED_ENV=debian_apt" in res.stdout
    assert "PKG_MGR=apt-get" in res.stdout


def test_installer_selects_pkg_in_native_termux_non_root(tmp_path: Path):
    bin_dir = tmp_path / "termux_bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "pkg", "#!/usr/bin/env bash\nexit 0\n")

    missing_os_release = tmp_path / "nonexistent_os_release"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:/bin:/usr/bin"
    env["MUSICVAULT_OS_RELEASE"] = str(missing_os_release)
    env["MUSICVAULT_EFFECTIVE_UID"] = "10329"

    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--detect-only"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SELECTED_ENV=native_termux" in res.stdout
    assert "PKG_MGR=pkg" in res.stdout


def test_installer_refuses_pkg_as_root_without_distro(tmp_path: Path):
    bin_dir = tmp_path / "only_pkg_bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "pkg", "#!/usr/bin/env bash\nexit 0\n")
    # Provide basic utilities via symlinks without exposing /usr/bin/apt-get
    for tool in ("id", "dirname", "pwd", "mkdir", "chmod", "cp", "cat"):
        real = subprocess.check_output(["which", tool], text=True).strip()
        (bin_dir / tool).symlink_to(real)

    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "MUSICVAULT_OS_RELEASE": str(tmp_path / "no_os_release"),
        "MUSICVAULT_EFFECTIVE_UID": "0",
    }
    res = subprocess.run(
        ["/bin/bash", str(INSTALL_SH), "--detect-only"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "Refusing to run Termux 'pkg' as root" in res.stderr


def test_installer_dry_run_system_editable_and_preserves_config(tmp_path: Path):
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=debian\nPRETTY_NAME="Debian Trixie"\n', encoding="utf-8")

    cfg_dir = tmp_path / ".config" / "musicvault"
    cfg_dir.mkdir(parents=True)
    env_file = cfg_dir / ".env"
    env_file.write_text("SONOVAULT_API_KEY=keep_my_existing_key\nDRY_RUN=True\n", encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["MUSICVAULT_CONFIG_DIR"] = str(cfg_dir)
    env["MUSICVAULT_OS_RELEASE"] = str(os_release)
    env["MUSICVAULT_EFFECTIVE_UID"] = "0"
    env["PYTHONPATH"] = str(REPO_ROOT)

    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--dry-run", "--system-editable"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "python3-setuptools" in res.stdout
    assert "python3-wheel" in res.stdout
    assert "pip install --no-deps --no-build-isolation --break-system-packages -e ." in res.stdout
    assert "Preserved existing configuration" in res.stdout
    assert env_file.read_text(encoding="utf-8") == "SONOVAULT_API_KEY=keep_my_existing_key\nDRY_RUN=True\n"


def test_installer_fails_clearly_when_required_imports_missing(tmp_path: Path):
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=debian\nPRETTY_NAME="Debian Trixie"\n', encoding="utf-8")

    fake_py = tmp_path / "fake_python3"
    _make_executable(
        fake_py,
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"-c\" ]]; then\n"
        "  echo '[✗] Missing required Python imports: mutagen (No module named mutagen)' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["MUSICVAULT_CONFIG_DIR"] = str(tmp_path / ".config" / "musicvault")
    env["MUSICVAULT_OS_RELEASE"] = str(os_release)
    env["MUSICVAULT_EFFECTIVE_UID"] = "0"
    env["MUSICVAULT_PYTHON_BIN"] = str(fake_py)
    # Run non-dry-run with mocked apt-get so import verification executes
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "apt-get", "#!/usr/bin/env bash\nexit 0\n")
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--system-editable"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "Missing required Python imports" in res.stderr


def test_wake_lock_reports_detection_separately_from_acquisition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    os_rel = tmp_path / "os-release"
    os_rel.write_text('ID=debian\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n', encoding="utf-8")

    # Reset cached status
    monkeypatch.setattr(termux, "_LAST_WAKE_LOCK_ACQUIRED", None)
    monkeypatch.setattr(termux, "_LAST_WAKE_LOCK_DETAIL", "not attempted")
    monkeypatch.setattr(termux, "find_wake_lock_binary", lambda name="termux-wake-lock": "/mock/termux-wake-lock")

    info_before = termux.detect_environment(os_rel)
    assert info_before.wake_lock_detected is True
    assert info_before.wake_lock_acquired is None
    assert "untested" in info_before.short_label()

    # Simulate PRoot failure when invoking host termux-wake-lock
    class FailedProc:
        returncode = 1
        stdout = ""
        stderr = "Termux:API IPC unavailable in PRoot"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FailedProc())
    ok, detail = termux.acquire_wake_lock_detailed()
    assert ok is False
    assert "acquisition failed" in detail

    info_after = termux.detect_environment(os_rel)
    assert info_after.wake_lock_detected is True
    assert info_after.wake_lock_acquired is False
    assert "wake-lock failed" in info_after.short_label()
