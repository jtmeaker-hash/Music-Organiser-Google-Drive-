import pytest

from musicvault.core.inventory import scan_root
from musicvault.db import Database
from musicvault.services.drive import DriveService
from musicvault.services.read_only import read_only_request
from musicvault.services.sheets import SheetsService


class ScriptedDrive:
    def __init__(self, items, fail=False):
        self.items = items
        self.fail = fail

    def scan_inventory(self, root):
        yield from self.items
        if self.fail:
            raise OSError("folder inaccessible")


def file(file_id, parent):
    return {"id": file_id, "name": "Same name.flac", "parents": [parent], "md5Checksum": "body"}


def test_root_publication_is_atomic_and_scoped(tmp_path):
    db = Database(tmp_path / "library.db")
    a, b = file("a", "source"), file("b", "vault")
    assert scan_root(db, ScriptedDrive([a, a]), "source", "source")["duplicates"] == 1
    assert scan_root(db, ScriptedDrive([b]), "vault", "destination")["seen"] == 1
    failed = scan_root(db, ScriptedDrive([file("c", "source")], fail=True), "source", "source")
    assert failed["status"] == "failed"
    assert db.get_track("c") is None
    assert db.get_track("a").is_live_source
    assert not db.get_track("b").is_live_source
    scan_root(db, ScriptedDrive([]), "source", "source")
    with db._get_connection() as conn:
        assert [tuple(row) for row in conn.execute("SELECT role,file_id FROM root_membership")] == [
            ("destination", "b")
        ]
    assert db.get_track("a") is not None


def test_write_boundary_denies_permissive_calls():
    with pytest.raises(RuntimeError):
        DriveService.__new__(DriveService).ensure_folder("root", "Genre", allow_live_writes=True)
    with pytest.raises(RuntimeError):
        DriveService.__new__(DriveService).move_file("id", "new", "old", allow_live_writes=True)
    with pytest.raises(RuntimeError):
        SheetsService.__new__(SheetsService).apply_batch_updates("sheet", [], allow_live_writes=True)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(RuntimeError):
            read_only_request(None, None, "https://example.invalid", method=method)
