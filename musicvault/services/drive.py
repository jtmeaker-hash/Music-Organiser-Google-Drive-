"""
Google Drive v3 API client for inventory scanning, bounded ranged audio reads,
opt-in temporary downloads with disk/budget guards, and folder inspection.
"""
import io
import json
import shutil
import sqlite3
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .read_only import read_only_request

from .range_reader import (
    BudgetExceededError,
    ByteBudgetTracker,
    DriveRangeReader,
    HydrationTransportError,
    InsufficientDiskSpaceError,
    make_requests_range_fetcher,
)

SUPPORTED_AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".aiff", ".aif",
    ".m4a", ".ogg", ".opus", ".aac",
)

DRIVE_MEDIA_URL_TEMPLATE = "https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"


class DriveService:
    """Wrapper around Google Drive v3 API."""

    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self.service = build("drive", "v3", credentials=credentials, requestBuilder=read_only_request)
        self._auth_session: Optional[AuthorizedSession] = None

    def _get_auth_session(self) -> AuthorizedSession:
        if self._auth_session is None:
            self._auth_session = AuthorizedSession(self.credentials)
        return self._auth_session

    def _enumerate_single_folder(
        self, folder_id: str, folder_path: List[str], corpus: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], List[tuple[str, str]], int, int]:
        """Enumerate all pages of a single Drive folder before committing its completion."""
        page_token = None
        tokens = set()
        audio_items: List[Dict[str, Any]] = []
        child_folders: List[tuple[str, str]] = []
        shortcuts = 0
        unsupported = 0

        while True:
            resp = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,modifiedTime,parents)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                **corpus,
                pageSize=1000,
                pageToken=page_token,
            ).execute(num_retries=3)

            if resp.get("incompleteSearch"):
                raise RuntimeError("Drive reported an incomplete folder listing.")
            for item in resp.get("files", []):
                mime = item.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    child_folders.append(
                        (item["id"], json.dumps([*folder_path, item.get("name", "")]))
                    )
                elif mime == "application/vnd.google-apps.shortcut":
                    shortcuts += 1
                else:
                    ext = Path(item.get("name", "")).suffix.lower()
                    if ext in SUPPORTED_AUDIO_EXTENSIONS:
                        item["scanned_parent"] = folder_id
                        item["scanned_path"] = [*folder_path, item.get("name", "")]
                        audio_items.append(item)
                    else:
                        unsupported += 1

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            if page_token in tokens:
                raise RuntimeError("Drive repeated a pagination token.")
            tokens.add(page_token)

        return audio_items, child_folders, shortcuts, unsupported

    def scan_inventory(
        self,
        root_folder_id: str,
        counters: Optional[Dict[str, int]] = None,
        db: Optional[Any] = None,
        scan_id: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        """
        Recursively scan folder for supported audio files (read-only metadata list).
        When `db` and `scan_id` are supplied, checkpoints completed folders in SQLite `scan_folders`
        so interrupted scans resume without re-listing already-completed folders.
        Yields file dicts: id, name, mimeType, size, md5Checksum, modifiedTime, parents, scanned_parent.
        """
        counters = counters if counters is not None else {}
        root = self.service.files().get(
            fileId=root_folder_id, fields="id,mimeType,driveId,trashed", supportsAllDrives=True,
        ).execute(num_retries=3)
        if root.get("trashed") or root.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError("Configured inventory root is not an accessible folder.")
        drive_id = root.get("driveId")
        corpus = {"corpora": "drive", "driveId": drive_id} if drive_id else {}

        if db is not None and scan_id is not None:
            with closing(db._get_connection()) as folders:
                with folders:
                    folders.execute(
                        "INSERT OR IGNORE INTO scan_folders(scan_id, folder_id, path, visited) VALUES (?, ?, '[]', 0)",
                        (int(scan_id), root_folder_id),
                    )
                while True:
                    next_folder = folders.execute(
                        """
                        SELECT sequence, folder_id, path FROM scan_folders
                        WHERE scan_id = ? AND visited = 0 ORDER BY sequence LIMIT 1
                        """,
                        (int(scan_id),),
                    ).fetchone()
                    if next_folder is None:
                        break
                    sequence, folder_id, path = next_folder["sequence"], next_folder["folder_id"], next_folder["path"]
                    folder_path = json.loads(path)
                    audio_items, child_folders, sc_cnt, un_cnt = self._enumerate_single_folder(
                        folder_id, folder_path, corpus
                    )
                    for item in audio_items:
                        yield item
                    if sc_cnt:
                        counters["shortcuts"] = counters.get("shortcuts", 0) + sc_cnt
                    if un_cnt:
                        counters["unsupported"] = counters.get("unsupported", 0) + un_cnt
                    with folders:
                        for child_id, child_path in child_folders:
                            folders.execute(
                                "INSERT OR IGNORE INTO scan_folders(scan_id, folder_id, path, visited) VALUES (?, ?, ?, 0)",
                                (int(scan_id), child_id, child_path),
                            )
                        folders.execute("UPDATE scan_folders SET visited = 1 WHERE sequence = ?", (sequence,))
            return

        # Keep the traversal frontier on disk so wide folder trees do not
        # accumulate in memory alongside a page of Drive results.
        with closing(sqlite3.connect("")) as folders:
            folders.execute("PRAGMA temp_store=FILE")
            folders.execute("""CREATE TABLE pending (
                sequence INTEGER PRIMARY KEY, folder_id TEXT UNIQUE, path TEXT, visited INTEGER DEFAULT 0
            )""")
            folders.execute("CREATE INDEX pending_visited ON pending(visited,sequence)")
            folders.execute("INSERT INTO pending(folder_id,path) VALUES (?, '[]')", (root_folder_id,))
            while True:
                next_folder = folders.execute("""
                    SELECT sequence,folder_id,path FROM pending WHERE visited=0 ORDER BY sequence LIMIT 1
                """).fetchone()
                if next_folder is None:
                    break
                sequence, folder_id, path = next_folder
                folders.execute("UPDATE pending SET visited=1 WHERE sequence=?", (sequence,))
                folder_path = json.loads(path)
                audio_items, child_folders, sc_cnt, un_cnt = self._enumerate_single_folder(
                    folder_id, folder_path, corpus
                )
                if sc_cnt:
                    counters["shortcuts"] = counters.get("shortcuts", 0) + sc_cnt
                if un_cnt:
                    counters["unsupported"] = counters.get("unsupported", 0) + un_cnt
                for child_id, child_path in child_folders:
                    folders.execute(
                        "INSERT OR IGNORE INTO pending(folder_id,path) VALUES (?,?)",
                        (child_id, child_path),
                    )
                for item in audio_items:
                    yield item

    def open_range_reader(
        self,
        file_id: str,
        budget: ByteBudgetTracker,
        known_size: int = 0,
        expected_md5: str = "",
        block_size: int = 65536,
        max_file_bytes: int = 2 * 1024 * 1024,
    ) -> io.BufferedReader:
        """
        Open a seekable bounded HTTP Range stream over a Google Drive audio file.
        Transfers only requested 64 KiB blocks and aborts immediately if Range is ignored.
        """
        quoted_id = urllib.parse.quote(file_id, safe="")
        url = DRIVE_MEDIA_URL_TEMPLATE.format(file_id=quoted_id)
        fetcher = make_requests_range_fetcher(self._get_auth_session(), url, budget=budget)
        raw_reader = DriveRangeReader(
            fetcher=fetcher,
            known_size=known_size,
            expected_md5=expected_md5,
            block_size=block_size,
            max_file_bytes=max_file_bytes,
        )
        return io.BufferedReader(raw_reader, buffer_size=block_size)

    def download_bounded_temp_file(
        self,
        file_id: str,
        dest_path: Path,
        budget: ByteBudgetTracker,
        expected_size: int = 0,
        max_full_bytes: int = 50 * 1024 * 1024,
        min_free_disk_bytes: int = 64 * 1024 * 1024,
    ) -> int:
        """
        Stream a full temporary file download ONLY when explicitly opted in by user.
        Enforces free-space check, per-file cap, and run byte budget; cleans partial file on error.
        """
        if expected_size > max_full_bytes:
            raise HydrationTransportError(
                f"File size ({expected_size} bytes) exceeds max full-download cap ({max_full_bytes} bytes)"
            )
        if expected_size > 0 and not budget.can_spend(expected_size):
            raise BudgetExceededError(
                f"Remaining byte budget ({budget.remaining_bytes}) insufficient for file ({expected_size})"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(str(dest_path.parent)).free
        required_free = max(min_free_disk_bytes, expected_size * 2)
        if free_bytes < required_free:
            raise InsufficientDiskSpaceError(
                f"Insufficient temporary disk space ({free_bytes} free < {required_free} required)"
            )

        quoted_id = urllib.parse.quote(file_id, safe="")
        url = DRIVE_MEDIA_URL_TEMPLATE.format(file_id=quoted_id)
        session = self._get_auth_session()
        transferred = 0
        resp = session.get(url, stream=True, timeout=30)
        try:
            if not resp.ok:
                raise HydrationTransportError(f"Drive media GET failed with HTTP {resp.status_code}")
            with dest_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    c_len = len(chunk)
                    transferred += c_len
                    budget.record(c_len)
                    if transferred > max_full_bytes:
                        raise HydrationTransportError(
                            f"Full download exceeded cap ({transferred} > {max_full_bytes} bytes)"
                        )
                    fh.write(chunk)
            return transferred
        except BaseException:
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            resp.close()

    def download_audio_file(self, file_id: str, dest_path: Path, chunk_size: int = 8 * 1024 * 1024) -> None:
        """Legacy helper: download file media to local destination path in chunks."""
        req = self.service.files().get_media(fileId=file_id)
        with dest_path.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, req, chunksize=chunk_size)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)

    def get_children_folders(self, parent_id: str) -> Dict[str, List[str]]:
        """Return map of folder name -> list of folder IDs under parent (read-only, shared-drive aware)."""
        found: Dict[str, List[str]] = {}
        corpus: Dict[str, Any] = {}
        files_api = self.service.files()
        if hasattr(files_api, "get"):
            try:
                root = files_api.get(
                    fileId=parent_id, fields="id,driveId,trashed", supportsAllDrives=True
                ).execute(num_retries=3)
                if isinstance(root, dict) and root.get("driveId"):
                    corpus = {"corpora": "drive", "driveId": root["driveId"]}
            except Exception:
                corpus = {}
        token = None
        tokens = set()
        while True:
            resp = files_api.list(
                q=f"'{parent_id}' in parents and trashed = false and mimeType = 'application/vnd.google-apps.folder'",
                fields="nextPageToken,incompleteSearch,files(id,name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                **corpus,
                pageSize=1000,
                pageToken=token,
            ).execute(num_retries=3)
            if resp.get("incompleteSearch"):
                raise RuntimeError("Drive reported an incomplete destination listing.")
            for f in resp.get("files", []):
                found.setdefault(f["name"], []).append(f["id"])
            token = resp.get("nextPageToken")
            if not token:
                break
            if token in tokens:
                raise RuntimeError("Drive repeated a destination listing page token.")
            tokens.add(token)
        return found

    def ensure_folder(self, parent_id: str, folder_name: str, allow_live_writes: bool = False) -> str:
        raise RuntimeError("Live Google Drive folder creation is disabled in Music Vault v2.0.1 and later.")

    def inspect_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch file metadata for pre-flight verification (read-only)."""
        return self.service.files().get(
            fileId=file_id,
            fields="id, name, parents, mimeType, trashed",
            supportsAllDrives=True,
        ).execute()

    def move_file(
        self, file_id: str, new_parent_id: str, old_parent_id: str, allow_live_writes: bool = False
    ) -> Dict[str, Any]:
        raise RuntimeError("Live Google Drive file moves are disabled in Music Vault v2.0.1 and later.")
