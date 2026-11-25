"""
Termux, PRoot, and Android system integration utilities.
Distinguishes wake-lock binary detection from actual wake-lock acquisition.
"""
import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple


_LAST_WAKE_LOCK_ACQUIRED: Optional[bool] = None
_LAST_WAKE_LOCK_DETAIL: str = "not attempted"


@dataclass
class EnvironmentInfo:
    """Structured runtime environment and wake-lock diagnostic status."""
    runtime: str  # 'debian_proot', 'ubuntu_proot', 'native_termux', 'standard_linux'
    distro_name: str
    is_root: bool
    in_tmux: bool
    tmux_available: bool
    wake_lock_binary: Optional[str]
    wake_lock_detected: bool
    wake_lock_acquired: Optional[bool]
    wake_lock_detail: str

    def short_label(self) -> str:
        """Compact honest status label for narrow/wide terminal headers."""
        env_map = {
            "debian_proot": "Debian PRoot",
            "ubuntu_proot": "Ubuntu PRoot",
            "native_termux": "Native Termux",
            "standard_linux": "Linux",
        }
        base = env_map.get(self.runtime, self.runtime)
        if self.wake_lock_acquired is True:
            wl = "wake-lock active"
        elif self.wake_lock_acquired is False:
            wl = "wake-lock failed"
        elif self.wake_lock_detected:
            wl = "wake-lock bin detected (untested)"
        else:
            wl = "no wake-lock"
        tmux_tag = ", tmux" if self.in_tmux else ""
        return f"{base} ({wl}{tmux_tag})"


def _read_os_release(os_release_path: Path = Path("/etc/os-release")) -> Tuple[str, str, str]:
    """Return (id, id_like, pretty_name) from /etc/os-release if readable."""
    if not os_release_path.exists():
        return "", "", ""
    distro_id = ""
    distro_like = ""
    pretty = ""
    try:
        for raw_line in os_release_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            val = v.strip().strip("'\"")
            if k == "ID":
                distro_id = val.lower()
            elif k == "ID_LIKE":
                distro_like = val.lower()
            elif k == "PRETTY_NAME":
                pretty = val
    except OSError:
        pass
    return distro_id, distro_like, pretty


def find_wake_lock_binary(binary_name: str = "termux-wake-lock") -> Optional[str]:
    """Locate Termux wake-lock/unlock binary if present and executable."""
    found = shutil.which(binary_name)
    if found:
        return found
    for candidate in (
        f"/data/data/com.termux/files/usr/bin/{binary_name}",
        f"/usr/bin/{binary_name}",
    ):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_termux() -> bool:
    """Detect if running on Android Termux or inside a Termux-hosted PRoot container."""
    return (
        os.path.exists("/data/data/com.termux")
        or "TERMUX_VERSION" in os.environ
        or "TERMUX_EXEC__PROC_SELF_EXE" in os.environ
        or find_wake_lock_binary() is not None
    )


def detect_environment(os_release_path: Path = Path("/etc/os-release")) -> EnvironmentInfo:
    """Inspect runtime distro, tmux presence, wake-lock binary detection, and last acquisition result."""
    distro_id, distro_like, pretty = _read_os_release(os_release_path)
    termux_host = is_termux()
    wake_bin = find_wake_lock_binary("termux-wake-lock")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0

    if termux_host and (distro_id == "debian" or "debian" in distro_like):
        runtime = "debian_proot"
    elif termux_host and (distro_id == "ubuntu" or "ubuntu" in distro_like):
        runtime = "ubuntu_proot"
    elif termux_host and not distro_id:
        runtime = "native_termux"
    elif termux_host:
        runtime = "debian_proot" if is_root else "native_termux"
    else:
        runtime = "standard_linux"

    return EnvironmentInfo(
        runtime=runtime,
        distro_name=pretty or distro_id or ("Termux" if termux_host else "POSIX Linux"),
        is_root=is_root,
        in_tmux=bool(os.environ.get("TMUX")),
        tmux_available=shutil.which("tmux") is not None,
        wake_lock_binary=wake_bin,
        wake_lock_detected=wake_bin is not None,
        wake_lock_acquired=_LAST_WAKE_LOCK_ACQUIRED,
        wake_lock_detail=_LAST_WAKE_LOCK_DETAIL,
    )


def acquire_wake_lock_detailed() -> Tuple[bool, str]:
    """
    Attempt to acquire Android wake-lock via Termux API.
    Returns (acquired_ok, diagnostic_detail) and updates cached status.
    """
    global _LAST_WAKE_LOCK_ACQUIRED, _LAST_WAKE_LOCK_DETAIL
    wake_lock_bin = find_wake_lock_binary("termux-wake-lock")
    if not wake_lock_bin:
        _LAST_WAKE_LOCK_ACQUIRED = False
        _LAST_WAKE_LOCK_DETAIL = "termux-wake-lock binary not found"
        return False, _LAST_WAKE_LOCK_DETAIL

    try:
        res = subprocess.run(
            [wake_lock_bin],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            _LAST_WAKE_LOCK_ACQUIRED = True
            _LAST_WAKE_LOCK_DETAIL = "acquired via termux-wake-lock"
            return True, _LAST_WAKE_LOCK_DETAIL
        stderr_short = (res.stderr or res.stdout or "").strip().splitlines()
        hint = stderr_short[0][:120] if stderr_short else f"exit code {res.returncode}"
        _LAST_WAKE_LOCK_ACQUIRED = False
        _LAST_WAKE_LOCK_DETAIL = f"binary detected but acquisition failed ({hint}); run termux-wake-lock in host Termux if in PRoot"
        return False, _LAST_WAKE_LOCK_DETAIL
    except Exception as exc:
        _LAST_WAKE_LOCK_ACQUIRED = False
        _LAST_WAKE_LOCK_DETAIL = f"binary detected but execution failed ({type(exc).__name__})"
        return False, _LAST_WAKE_LOCK_DETAIL


def acquire_wake_lock() -> bool:
    """Acquire Android wake-lock via Termux API to prevent CPU sleep during long runs."""
    ok, _ = acquire_wake_lock_detailed()
    return ok


def release_wake_lock() -> bool:
    """Release Android wake-lock."""
    global _LAST_WAKE_LOCK_ACQUIRED, _LAST_WAKE_LOCK_DETAIL
    unlock_bin = find_wake_lock_binary("termux-wake-unlock")
    if not unlock_bin:
        return False
    try:
        res = subprocess.run([unlock_bin], capture_output=True, timeout=5, check=False)
        if res.returncode == 0:
            _LAST_WAKE_LOCK_ACQUIRED = None
            _LAST_WAKE_LOCK_DETAIL = "released"
            return True
        return False
    except Exception:
        return False


@contextlib.contextmanager
def termux_wake_lock() -> Iterator[bool]:
    """Context manager for acquiring wake-lock during batch execution."""
    acquired = acquire_wake_lock()
    try:
        yield acquired
    finally:
        if acquired:
            release_wake_lock()
