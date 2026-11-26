"""
Google Drive v3 API client for inventory scanning, bounded ranged audio reads,
opt-in temporary downloads with disk/budget guards, and folder inspection.
"""
import io
import shutil
import urllib.parse
from collections import deque
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

    def scan_inventory(self, root_folder_id: str, counters: Optional[Dict[str, int]] = None) -> Iterator[Dict[str, Any]]:
        """
        Recursively scan folder for supported audio files (read-only metadata list).
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
        queue = deque([(root_folder_id, ())])
        seen_folders = set()

        while queue:
            folder_id, folder_path = queue.popleft()
            if folder_id in seen_folders:
                continue
            seen_folders.add(folder_id)

            page_token = None
            tokens = set()
            while True:
                resp = self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,modifiedTime,parents)",
                    supportsAllDrives=True, includeItemsFromAllDrives=True, **corpus,
                    pageSize=1000,
                    pageToken=page_token,
                ).execute(num_retries=3)

                if resp.get("incompleteSearch"):
                    raise RuntimeError("Drive reported an incomplete folder listing.")
                for item in resp.get("files", []):
                    mime = item.get("mimeType", "")
                    if mime == "application/vnd.google-apps.folder":
                        queue.append((item["id"], (*folder_path, item.get("name", ""))))
                    elif mime == "application/vnd.google-apps.shortcut":
                        counters["shortcuts"] = counters.get("shortcuts", 0) + 1
                    else:
                        ext = Path(item.get("name", "")).suffix.lower()
                        if ext in SUPPORTED_AUDIO_EXTENSIONS:
                            item["scanned_parent"] = folder_id
                            item["scanned_path"] = [*folder_path, item.get("name", "")]
                            yield item
                        else:
                            counters["unsupported"] = counters.get("unsupported", 0) + 1

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
                if page_token in tokens:
                    raise RuntimeError("Drive repeated a pagination token.")
                tokens.add(page_token)

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
        """Return map of folder name -> list of folder IDs under parent (read-only)."""
        found: Dict[str, List[str]] = {}
        token = None
        while True:
            resp = self.service.files().list(
                q=f"'{parent_id}' in parents and trashed = false and mimeType = 'application/vnd.google-apps.folder'",
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=token,
            ).execute(num_retries=3)
            for f in resp.get("files", []):
                found.setdefault(f["name"], []).append(f["id"])
            token = resp.get("nextPageToken")
            if not token:
                break
        return found

    def ensure_folder(self, parent_id: str, folder_name: str, allow_live_writes: bool = False) -> str:
        raise RuntimeError("Live Google Drive folder creation is disabled in Music Vault v2.0.1 and later.")

    def inspect_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch file metadata for pre-flight verification (read-only)."""
        return self.service.files().get(
            fileId=file_id,
            fields="id, name, parents, mimeType, trashed",
        ).execute()

    def move_file(
        self, file_id: str, new_parent_id: str, old_parent_id: str, allow_live_writes: bool = False
    ) -> Dict[str, Any]:
        raise RuntimeError("Live Google Drive file moves are disabled in Music Vault v2.0.1 and later.")
