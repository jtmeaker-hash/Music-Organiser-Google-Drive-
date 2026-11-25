"""
Pytest safety fixtures for Music Vault Manager.
Guarantees every test runs against an isolated temporary HOME/config/DB and blocks real network calls.
"""
import socket
from pathlib import Path
from typing import Dict, Any, List
import pytest


_SENSITIVE_ENV_VARS = (
    "SONOVAULT_API_KEY",
    "AUDIOMETA_API_KEY",
    "DISCOGS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLIENT_SECRETS_FILE",
    "SOURCE_FOLDER_ID",
    "VAULT_FOLDER_ID",
    "SHEET_ID",
    "DRY_RUN",
    "ALLOW_PROVISIONAL_MOVES",
    "PROVIDER_TERMS_CLEARED",
    "SAVE_MEDIUM",
)


@pytest.fixture(autouse=True)
def isolated_musicvault_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Force isolated HOME and config dir and block real external network sockets."""
    fake_home = tmp_path / "isolated_home"
    fake_config = fake_home / ".config" / "musicvault"
    fake_home.mkdir(parents=True, exist_ok=True)
    fake_config.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("MUSICVAULT_CONFIG_DIR", str(fake_config))

    for env_key in _SENSITIVE_ENV_VARS:
        monkeypatch.delenv(env_key, raising=False)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _blocked_connect(self, address):
        if isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        if isinstance(address, (str, bytes)):
            # Allow local AF_UNIX sockets if needed by Python runtime
            return real_connect(self, address)
        raise RuntimeError(f"Blocked real external network connection in test: {address!r}")

    def _blocked_connect_ex(self, address):
        if isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect_ex(self, address)
        raise RuntimeError(f"Blocked real external network connection in test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect_ex)

    # Prevent unit tests from invoking host Android /data/data/com.termux/files/usr/bin/termux-wake-lock
    from musicvault import termux as _mv_termux
    monkeypatch.setattr(_mv_termux, "find_wake_lock_binary", lambda *a, **kw: None)

    yield fake_config


@pytest.fixture
def mock_drive_inventory() -> List[Dict[str, Any]]:
    """Sample mocked Google Drive inventory records."""
    return [
        {
            "id": "DRV_LIVE_001",
            "name": "Act of Rage - Living Fast.flac",
            "mimeType": "audio/flac",
            "size": "28450112",
            "md5Checksum": "ec6ec53fa1eb062fd5d840d6d7efce63",
            "modifiedTime": "2026-11-20T12:00:00Z",
            "parents": ["SRC_FOLDER_1"],
            "scanned_parent": "SRC_FOLDER_1",
        },
        {
            "id": "DRV_LIVE_002",
            "name": "01 Beneath The Lights - Darren Styles Remix.wav",
            "mimeType": "audio/wav",
            "size": "45120000",
            "md5Checksum": "aa11bb22cc33dd44ee55ff6677889900",
            "modifiedTime": "2026-11-20T12:05:00Z",
            "parents": ["SRC_FOLDER_1"],
            "scanned_parent": "SRC_FOLDER_1",
        },
        {
            "id": "DRV_LIVE_003",
            "name": "01 Clarity.wav",
            "mimeType": "audio/wav",
            "size": "39100000",
            "md5Checksum": "11223344556677889900aabbccddeeff",
            "modifiedTime": "2026-11-20T12:10:00Z",
            "parents": ["SRC_FOLDER_1"],
            "scanned_parent": "SRC_FOLDER_1",
        },
    ]


@pytest.fixture
def mock_tracks_sheet() -> Dict[str, Any]:
    """Sample mocked Google Sheets Tracks tab header and rows."""
    headers = [
        "Title", "Artist", "Genre", "Subgenre", "BPM", "Key",
        "Energy", "Rating", "Duration", "ISRC", "Year", "Status",
        "Added", "Folder", "Drive ID", "Notes",
        "Review Star", "Review Reasons", "BPM State", "Key State",
        "Genre State", "Subgenre State", "Metadata Lock JSON",
        "Metadata Evidence Summary", "Last Review UTC", "Review Status",
        "Secondary Influence",
    ]
    rows = [
        [
            "Living Fast", "Act of Rage", "Hardstyle", "Rawstyle", "158.0", "11A",
            "", "", "240.0", "", "", "", "", "", "DRV_LIVE_001", "",
        ],
    ]
    return {"headers": headers, "rows": rows}
