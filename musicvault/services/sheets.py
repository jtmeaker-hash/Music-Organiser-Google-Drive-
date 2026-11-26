"""
Google Sheets v4 API client for read-only master library index loading, header schema
verification, duplicate/missing Drive ID detection, and audited write helpers.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .read_only import read_only_request

from ..core.matcher import norm
from ..core.rules import COL, EXTRA_HEADERS, FIELDS, parse_lock_states

_VALID_DRIVE_ID_HEADERS = {
    "id", "fileid", "driveid", "drivefileid", "googledriveid", "googlefileid", "trackid",
}
_EXPECTED_FIELD_HEADER_ALIASES = {
    2: {"genre", "parentgenre", "primarygenre"},
    3: {"subgenre", "subgenres", "secondarygenre", "style"},
    4: {"bpm", "tempo", "bpmfull"},
    5: {"key", "camelot", "camelotkey", "musicalkey", "initialkey"},
}
_DRIVE_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,80}$")


@dataclass
class VerifiedSheetIndex:
    """Validated read-only snapshot of the Google Sheets 'Tracks' tab."""
    schema_valid: bool
    schema_error: str = ""
    headers: List[str] = field(default_factory=list)
    unique_rows_by_id: Dict[str, Tuple[int, List[str]]] = field(default_factory=dict)
    duplicate_ids: Dict[str, List[int]] = field(default_factory=dict)
    missing_id_rows: List[int] = field(default_factory=list)
    verified_identities: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    total_data_rows: int = 0
    drive_id_col: int = 14


def validate_and_index_tracks_sheet(
    header_row: List[Any], data_rows: List[List[Any]]
) -> VerifiedSheetIndex:
    """
    Validate real Tracks!A1:AA1 headers and index Tracks!A2:AA rows by exact Drive file ID.
    Detects incompatible headers, missing IDs, and duplicate Drive IDs without guessing rows.
    """
    headers = [str(x or "").strip() for x in (header_row or [])]
    if not any(headers):
        return VerifiedSheetIndex(
            schema_valid=False,
            schema_error="INCOMPATIBLE_HEADERS: Tracks!A1:AA1 header row is empty or missing",
            headers=headers,
        )

    padded_headers = headers + [""] * max(0, 27 - len(headers))

    # 1. Verify Drive File ID column at index 14 (Col O) vs all columns A:P
    explicit_id_cols = [
        idx for idx, h in enumerate(padded_headers[:16])
        if norm(h) in ("driveid", "fileid", "drivefileid", "googledriveid")
    ]
    if explicit_id_cols and 14 not in explicit_id_cols:
        return VerifiedSheetIndex(
            schema_valid=False,
            schema_error=f"INCOMPATIBLE_HEADERS: Drive ID header found at column index {explicit_id_cols[0]}, expected index 14 (Col O)",
            headers=headers,
        )

    col_o_norm = norm(padded_headers[14])
    if col_o_norm and col_o_norm not in _VALID_DRIVE_ID_HEADERS:
        return VerifiedSheetIndex(
            schema_valid=False,
            schema_error=f"INCOMPATIBLE_HEADERS: Column O (index 14) header is '{padded_headers[14]}', expected Drive ID",
            headers=headers,
        )
    if not col_o_norm:
        # Legacy blank O1 header allowed ONLY if populated O2:O cells look like Drive file IDs
        sample_o = [str(r[14]).strip() for r in data_rows if len(r) > 14 and str(r[14] or "").strip()][:10]
        if sample_o and not all(_DRIVE_ID_TOKEN_RE.match(v) for v in sample_o):
            return VerifiedSheetIndex(
                schema_valid=False,
                schema_error="INCOMPATIBLE_HEADERS: Column O header is blank and values do not match Drive file ID format",
                headers=headers,
            )

    # 2. Verify core metadata headers C1:F1 (indices 2..5) when non-empty
    for col_idx, allowed in _EXPECTED_FIELD_HEADER_ALIASES.items():
        h_norm = norm(padded_headers[col_idx])
        if h_norm and h_norm not in allowed:
            return VerifiedSheetIndex(
                schema_valid=False,
                schema_error=f"INCOMPATIBLE_HEADERS: Column index {col_idx} header '{padded_headers[col_idx]}' conflicts with expected schema",
                headers=headers,
            )

    # 3. Verify review columns Q1:AA1 (indices 16..26) are empty or match EXTRA_HEADERS
    for offset, expected_h in enumerate(EXTRA_HEADERS):
        actual_h = padded_headers[16 + offset]
        if actual_h and actual_h != expected_h:
            return VerifiedSheetIndex(
                schema_valid=False,
                schema_error=f"INCOMPATIBLE_REVIEW_HEADERS: Column index {16 + offset} has '{actual_h}', expected '{expected_h}'",
                headers=headers,
            )

    # 4. Detect optional explicitly labeled Artist and Title columns in A1:P1
    artist_col: Optional[int] = None
    title_col: Optional[int] = None
    for idx in range(16):
        if idx in (2, 3, 4, 5, 14):
            continue
        hn = norm(padded_headers[idx])
        if hn in ("artist", "artists", "primaryartist") and artist_col is None:
            artist_col = idx
        elif hn in ("title", "tracktitle", "songtitle") and title_col is None:
            title_col = idx

    # 5. Index rows starting at real sheet row 2 (never using fake row 999)
    seen_rows: Dict[str, List[Tuple[int, List[str]]]] = {}
    missing_id_rows: List[int] = []

    for offset, raw_r in enumerate(data_rows):
        row_num = offset + 2
        str_row = [str(cell) if cell is not None else "" for cell in raw_r]
        if not any(c.strip() for c in str_row):
            continue
        fid = str_row[14].strip() if len(str_row) > 14 else ""
        if not fid:
            missing_id_rows.append(row_num)
            continue
        padded_r = (str_row + [""] * max(0, 27 - len(str_row)))[:27]
        seen_rows.setdefault(fid, []).append((row_num, padded_r))

    unique_by_id: Dict[str, Tuple[int, List[str]]] = {}
    duplicate_ids: Dict[str, List[int]] = {}
    verified_identities: Dict[str, Tuple[str, str]] = {}

    for fid, entries in seen_rows.items():
        if len(entries) > 1:
            duplicate_ids[fid] = [r_num for r_num, _ in entries]
        else:
            r_num, r_vals = entries[0]
            unique_by_id[fid] = (r_num, r_vals)
            if artist_col is not None and title_col is not None:
                s_art = r_vals[artist_col].strip()
                s_tit = r_vals[title_col].strip()
                if s_art and s_tit:
                    verified_identities[fid] = (s_art, s_tit)

    return VerifiedSheetIndex(
        schema_valid=True,
        headers=headers,
        unique_rows_by_id=unique_by_id,
        duplicate_ids=duplicate_ids,
        missing_id_rows=missing_id_rows,
        verified_identities=verified_identities,
        total_data_rows=len(data_rows),
    )


class SheetsService:
    """Wrapper around Google Sheets v4 API."""

    def __init__(self, credentials: Credentials):
        self.service = build("sheets", "v4", credentials=credentials, requestBuilder=read_only_request)

    def fetch_header_row(self, spreadsheet_id: str, range_name: str = "Tracks!A1:AA1") -> List[Any]:
        """Fetch header row A1:AA1 from master spreadsheet (read-only)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute()
        vals = resp.get("values", [[]])
        return vals[0] if vals else []

    def fetch_master_rows(self, spreadsheet_id: str, range_name: str = "Tracks!A2:AA") -> List[List[Any]]:
        """Fetch all track data rows from master spreadsheet (read-only)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute()
        return resp.get("values", [])

    def load_verified_index(self, spreadsheet_id: str) -> VerifiedSheetIndex:
        """Fetch headers + rows (read-only) and validate schema, duplicate IDs, and missing IDs."""
        header_row = self.fetch_header_row(spreadsheet_id)
        data_rows = self.fetch_master_rows(spreadsheet_id)
        return validate_and_index_tracks_sheet(header_row, data_rows)

    def fetch_row(self, spreadsheet_id: str, row_num: int) -> List[Any]:
        """Fetch a single row (columns A:AA)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"Tracks!A{row_num}:AA{row_num}",
        ).execute()
        vals = resp.get("values", [[]])
        return vals[0] if vals else []

    def verify_headers(self, spreadsheet_id: str) -> bool:
        """Verify that review headers in Q1:AA1 are empty or match expected schema."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Tracks!Q1:AA1",
        ).execute()
        header = resp.get("values", [[]])[0]
        header = header + [""] * (len(EXTRA_HEADERS) - len(header))
        return not any(v and v != expected for v, expected in zip(header, EXTRA_HEADERS))

    def create_pre_write_backup(
        self,
        spreadsheet_id: str,
        rows_to_backup: List[Tuple[int, List[Any]]],
        backup_dir: Path,
    ) -> Path:
        """Create a local JSON backup of original rows before any future approved update."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        backup_path = backup_dir / f"music_vault_backup_{timestamp}.json"
        data = {
            "spreadsheet_id": spreadsheet_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "rows": [{"row": r_num, "values": vals} for r_num, vals in rows_to_backup],
        }
        with backup_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return backup_path

    def apply_batch_updates(
        self,
        spreadsheet_id: str,
        updates: List[Tuple[int, List[Any], List[Any]]],
        allow_live_writes: bool = False,
    ) -> int:
        raise RuntimeError("Live Google Sheets writes are disabled in MusicVault.")
