"""
Termux and Android system integration utilities.
Provides wake-lock support to keep processing alive in the background.
"""
import shutil
import subprocess
import os
import contextlib
from typing import Iterator


def is_termux() -> bool:
    """Detect if running under Termux or Termux-PRoot environment."""
    return (
        os.path.exists("/data/data/com.termux")
        or "TERMUX_VERSION" in os.environ
        or "TERMUX_EXEC__PROC_SELF_EXE" in os.environ
        or shutil.which("termux-wake-lock") is not None
    )


def acquire_wake_lock() -> bool:
    """Acquire Android wake-lock via Termux API to prevent CPU sleep during long runs."""
    wake_lock_bin = shutil.which("termux-wake-lock")
    if not wake_lock_bin:
        # Check standard Termux paths
        candidates = [
            "/data/data/com.termux/files/usr/bin/termux-wake-lock",
            "/usr/bin/termux-wake-lock"
        ]
        for c in candidates:
            if os.path.exists(c) and os.access(c, os.X_OK):
                wake_lock_bin = c
                break
    if not wake_lock_bin:
        return False
    try:
        res = subprocess.run([wake_lock_bin], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


def release_wake_lock() -> bool:
    """Release Android wake-lock."""
    unlock_bin = shutil.which("termux-wake-unlock")
    if not unlock_bin:
        candidates = [
            "/data/data/com.termux/files/usr/bin/termux-wake-unlock",
            "/usr/bin/termux-wake-unlock"
        ]
        for c in candidates:
            if os.path.exists(c) and os.access(c, os.X_OK):
                unlock_bin = c
                break
    if not unlock_bin:
        return False
    try:
        res = subprocess.run([unlock_bin], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


@contextlib.contextmanager
def termux_wake_lock() -> Iterator[bool]:
    """Context manager for acquiring wake lock during batch execution."""
    acquired = acquire_wake_lock()
    try:
        yield acquired
    finally:
        if acquired:
            release_wake_lock()
