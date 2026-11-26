"""Advisory lock shared by MusicVault CLI writers and the migration entry point."""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def database_lock(path: Path):
    lock_path = path.resolve().with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another MusicVault process holds the database lock.") from exc
        yield
    finally:
        os.close(fd)
