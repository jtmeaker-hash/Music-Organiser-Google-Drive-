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


def test_drive_paginates_shared_nested_folders_and_ignores_shortcut():
    service = DriveService.__new__(DriveService)
    calls = []

    class Request:
        def __init__(self, response):
            self.response = response

        def execute(self, **_kwargs):
            return self.response

    class API:
        def files(self):
            return self

        def get(self, **_kwargs):
            return Request({"mimeType": "application/vnd.google-apps.folder", "driveId": "shared"})

        def list(self, **kwargs):
            calls.append(kwargs)
            if "'folder'" in kwargs["q"]:
                return Request({"files": [file("nested-id", "folder")]})
            if kwargs["pageToken"] == "second":
                return Request({"files": [
                    {"id": "folder", "name": "Nested", "mimeType": "application/vnd.google-apps.folder"},
                    {"id": "shortcut", "name": "Shortcut.flac", "mimeType": "application/vnd.google-apps.shortcut"},
                    {"id": "unsupported", "name": "data.zip"},
                ]})
            return Request({"nextPageToken": "second", "files": [file("root-id", "root")]})

    service.service = API()
    counts = {}
    result = list(service.scan_inventory("root", counts))
    assert [row["id"] for row in result] == ["root-id", "nested-id"]
    assert result[1]["scanned_path"] == ["Nested", "Same name.flac"]
    assert counts == {"shortcuts": 1, "unsupported": 1}
    assert all(call["driveId"] == "shared" and call["includeItemsFromAllDrives"] for call in calls)


def test_google_discovery_mutation_requests_are_blocked():
    from google.auth.credentials import AnonymousCredentials

    drive = DriveService(AnonymousCredentials())
    sheets = SheetsService(AnonymousCredentials())
    assert drive.service.files().get(fileId="synthetic").method == "GET"
    with pytest.raises(RuntimeError, match="disabled"):
        drive.service.files().update(fileId="synthetic", body={"name": "changed"})
    with pytest.raises(RuntimeError, match="disabled"):
        drive.service.files().delete(fileId="synthetic")
    with pytest.raises(RuntimeError, match="disabled"):
        sheets.service.spreadsheets().values().batchUpdate(spreadsheetId="synthetic", body={})
