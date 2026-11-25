"""
Google Drive v3 API client for inventory scanning, folder creation, and safe moves.
"""
from collections import deque
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Any
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials

SUPPORTED_AUDIO_EXTENSIONS = (
    '.mp3', '.wav', '.flac', '.aiff', '.aif',
    '.m4a', '.ogg', '.opus', '.aac'
)


class DriveService:
    """Wrapper around Google Drive v3 API."""

    def __init__(self, credentials: Credentials):
        self.service = build('drive', 'v3', credentials=credentials)

    def scan_inventory(self, root_folder_id: str) -> Iterator[Dict[str, Any]]:
        """
        Recursively scan folder for supported audio files.
        Yields file dicts: id, name, mimeType, size, md5Checksum, modifiedTime, parent_id.
        """
        queue = deque([root_folder_id])
        seen_folders = set()

        while queue:
            folder_id = queue.popleft()
            if folder_id in seen_folders:
                continue
            seen_folders.add(folder_id)

            page_token = None
            while True:
                resp = self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime, parents)",
                    pageSize=1000,
                    pageToken=page_token
                ).execute(num_retries=3)

                for item in resp.get('files', []):
                    mime = item.get('mimeType', '')
                    if mime == 'application/vnd.google-apps.folder':
                        queue.append(item['id'])
                    else:
                        ext = Path(item.get('name', '')).suffix.lower()
                        if ext in SUPPORTED_AUDIO_EXTENSIONS:
                            item['scanned_parent'] = folder_id
                            yield item

                page_token = resp.get('nextPageToken')
                if not page_token:
                    break

    def download_audio_file(self, file_id: str, dest_path: Path, chunk_size: int = 8 * 1024 * 1024) -> None:
        """Download file media to local destination path in chunks."""
        req = self.service.files().get_media(fileId=file_id)
        with dest_path.open('wb') as fh:
            downloader = MediaIoBaseDownload(fh, req, chunksize=chunk_size)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)

    def get_children_folders(self, parent_id: str) -> Dict[str, List[str]]:
        """Return map of folder name -> list of folder IDs under parent."""
        found: Dict[str, List[str]] = {}
        token = None
        while True:
            resp = self.service.files().list(
                q=f"'{parent_id}' in parents and trashed = false and mimeType = 'application/vnd.google-apps.folder'",
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=token
            ).execute(num_retries=3)
            for f in resp.get('files', []):
                found.setdefault(f['name'], []).append(f['id'])
            token = resp.get('nextPageToken')
            if not token:
                break
        return found

    def ensure_folder(self, parent_id: str, folder_name: str) -> str:
        """
        Ensure folder exists under parent. If multiple exist, raises error to prevent ambiguity.
        """
        folders = self.get_children_folders(parent_id)
        ids = folders.get(folder_name, [])
        if len(ids) > 1:
            raise RuntimeError(f"Ambiguous duplicate folder detected on Google Drive: '{folder_name}'")
        if ids:
            return ids[0]

        # Create new folder
        meta = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        res = self.service.files().create(body=meta, fields='id').execute()
        return res['id']

    def inspect_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch file metadata for pre-flight verification before moving."""
        return self.service.files().get(
            fileId=file_id,
            fields='id, name, parents, mimeType, trashed'
        ).execute()

    def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str) -> Dict[str, Any]:
        """
        Move file on Google Drive by updating parents. Never downloads, re-uploads, or transcodes.
        """
        return self.service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parent_id,
            fields='id, parents'
        ).execute()
