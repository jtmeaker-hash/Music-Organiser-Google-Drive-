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

from ..core.artist_rules import (
    ARTIST_RULES_HEADERS,
    ARTIST_RULES_TAB_NAME,
    ArtistRule,
    artist_rule_to_sheet_row,
    parse_artist_rules_sheet,
)
from ..core.matcher import norm
from ..core.rules import EXTRA_HEADERS

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
    columns: Dict[int, int] = field(default_factory=dict)


def resolve_tracks_headers(header_row: List[Any]) -> VerifiedSheetIndex:
    """Map explicit aliases to the canonical review layout used by the rules engine."""
    headers = [str(value or "").strip() for value in header_row]
    padded = headers + [""] * max(0, 27 - len(headers))
    if not any(headers):
        return VerifiedSheetIndex(False, "INCOMPATIBLE_HEADERS: missing Tracks header")
    for i, expected in enumerate(EXTRA_HEADERS, 16):
        if padded[i] and padded[i] != expected:
            return VerifiedSheetIndex(False, f"INCOMPATIBLE_REVIEW_HEADERS: column {i + 1}", headers)
    normalized = [norm(header) for header in padded]
    columns = {}
    aliases = {
        **_EXPECTED_FIELD_HEADER_ALIASES,
        14: _VALID_DRIVE_ID_HEADERS,
        0: {"title", "tracktitle", "songtitle"},
        1: {"artist", "artists", "primaryartist"},
        11: {"processingstatus", "status"},
        12: {"analysisconfidence", "confidence"},
        15: {"notesoverride", "notes", "override", "manualoverride"},
    }
    for canonical, accepted in aliases.items():
        matches = [i for i, label in enumerate(normalized) if label in accepted and not 16 <= i <= 26]
        if len(matches) > 1:
            return VerifiedSheetIndex(False, f"INCOMPATIBLE_HEADERS: ambiguous aliases for field {canonical}", headers)
        if matches:
            columns[canonical] = matches[0]
        elif canonical in (2, 3, 4, 5, 14):
            if normalized[canonical]:
                return VerifiedSheetIndex(False, f"INCOMPATIBLE_HEADERS: missing field at column {canonical + 1}", headers)
            columns[canonical] = canonical
    if len(set(columns.values())) != len(columns):
        return VerifiedSheetIndex(False, "INCOMPATIBLE_HEADERS: overlapping field mappings", headers)
    return VerifiedSheetIndex(True, headers=headers, drive_id_col=columns[14], columns=columns)


def normalize_tracks_row(raw, index):
    original = [str(cell) if cell is not None else "" for cell in raw]
    padded = original + [""] * max(0, 27 - len(original))
    canonical = padded[:27]
    for opt_slot in (0, 1, 11, 12, 15):
        if opt_slot not in index.columns and opt_slot < len(index.headers) and index.headers[opt_slot].strip():
            canonical[opt_slot] = ""
    for dest, source in index.columns.items():
        canonical[dest] = padded[source] if source < len(padded) else ""
    return canonical


def validate_and_index_tracks_sheet(
    header_row: List[Any], data_rows: List[List[Any]]
) -> VerifiedSheetIndex:
    """Validate the Tracks layout and index exact IDs, excluding ambiguous rows."""
    index = resolve_tracks_headers(header_row)
    if not index.schema_valid:
        return index
    drive_col, headers = index.drive_id_col, index.headers
    if drive_col >= len(headers) or not headers[drive_col]:
        sample = [str(r[drive_col]).strip() for r in data_rows
                  if len(r) > drive_col and str(r[drive_col] or "").strip()][:10]
        if sample and not all(_DRIVE_ID_TOKEN_RE.fullmatch(value) for value in sample):
            return VerifiedSheetIndex(False, "INCOMPATIBLE_HEADERS: invalid legacy Drive IDs", headers)

    # 5. Index rows starting at real sheet row 2 (never using fake row 999)
    seen_rows: Dict[str, List[Tuple[int, List[str]]]] = {}
    missing_id_rows: List[int] = []

    for offset, raw_r in enumerate(data_rows):
        row_num = offset + 2
        str_row = [str(cell) if cell is not None else "" for cell in raw_r]
        if not any(c.strip() for c in str_row):
            continue
        fid = str_row[drive_col].strip() if len(str_row) > drive_col else ""
        if not fid:
            missing_id_rows.append(row_num)
            continue
        padded_r = normalize_tracks_row(str_row, index)
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
            if 1 in index.columns and 0 in index.columns:
                s_art = r_vals[1].strip()
                s_tit = r_vals[0].strip()
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
        drive_id_col=drive_col, columns=index.columns,
    )


def _col_letter(index_1_based: int) -> str:
    result = ""
    idx = max(1, int(index_1_based))
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


class SheetsService:
    """Wrapper around Google Sheets v4 API."""

    def __init__(self, credentials: Credentials):
        self.service = build("sheets", "v4", credentials=credentials, requestBuilder=read_only_request)

    def fetch_header_row(self, spreadsheet_id: str, range_name: str = "Tracks!1:1") -> List[Any]:
        """Fetch header row from master spreadsheet (read-only)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute(num_retries=3)
        vals = resp.get("values", [[]])
        return vals[0] if vals else []

    def fetch_master_rows(self, spreadsheet_id: str, range_name: str = "Tracks!A2:AA") -> List[List[Any]]:
        """Fetch all track data rows from master spreadsheet (read-only)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute(num_retries=3)
        return resp.get("values", [])

    def iter_tracks_rows(self, spreadsheet_id: str, last_column: str, batch_size: int = 1000):
        """Yield bounded pages using the declared grid bounds, including gaps."""
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties(rowCount)))",
        ).execute(num_retries=3)
        tracks = next((s["properties"] for s in metadata.get("sheets", [])
                       if s.get("properties", {}).get("title") == "Tracks"), None)
        if not tracks:
            raise ValueError("Tracks sheet is missing.")
        last_row = int(tracks["gridProperties"]["rowCount"])
        for start in range(2, last_row + 1, batch_size):
            end = min(last_row, start + batch_size - 1)
            rows = self.fetch_master_rows(spreadsheet_id, f"Tracks!A{start}:{last_column}{end}")
            yield from enumerate(rows, start)

    def load_verified_index(self, spreadsheet_id: str) -> VerifiedSheetIndex:
        """Fetch headers + rows (read-only) and validate schema, duplicate IDs, and missing IDs."""
        header_row = self.fetch_header_row(spreadsheet_id, "Tracks!1:1")
        last_col = _col_letter(max(27, len(header_row)))
        data_rows = self.fetch_master_rows(spreadsheet_id, f"Tracks!A2:{last_col}")
        return validate_and_index_tracks_sheet(header_row, data_rows)

    def fetch_row(self, spreadsheet_id: str, row_num: int) -> List[Any]:
        """Fetch a single row (columns A:AA)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"Tracks!A{row_num}:AA{row_num}",
        ).execute(num_retries=3)
        vals = resp.get("values", [[]])
        return vals[0] if vals else []

    def verify_headers(self, spreadsheet_id: str) -> bool:
        """Verify that review headers in Q1:AA1 are empty or match expected schema."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Tracks!Q1:AA1",
        ).execute(num_retries=3)
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

    def load_artist_rules_tab(
        self, spreadsheet_id: str, tab_name: str = ARTIST_RULES_TAB_NAME
    ) -> Tuple[List[ArtistRule], List[str]]:
        """
        Read-only fetch of the optional `Artist_Rules` tab from Google Sheets.
        Returns `([], [])` cleanly if the `Artist_Rules` tab has not been created yet.
        """
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title))",
        ).execute(num_retries=3)
        titles = {
            s.get("properties", {}).get("title", "")
            for s in metadata.get("sheets", [])
        }
        if tab_name not in titles:
            return [], []
        header_row = self.fetch_header_row(spreadsheet_id, f"'{tab_name}'!1:1")
        last_col = _col_letter(max(len(ARTIST_RULES_HEADERS), len(header_row)))
        data_rows = self.fetch_master_rows(spreadsheet_id, f"'{tab_name}'!A2:{last_col}")
        return parse_artist_rules_sheet(header_row, data_rows)

    @staticmethod
    def build_artist_rules_tab_payload(rules: List[ArtistRule]) -> List[List[str]]:
        """Build complete 2D sheet grid (`header + rows`) for staging/exporting the `Artist_Rules` tab."""
        return [list(ARTIST_RULES_HEADERS)] + [artist_rule_to_sheet_row(r) for r in rules]

    def apply_batch_updates(
        self,
        spreadsheet_id: str,
        updates: List[Tuple[int, List[Any], List[Any]]],
        allow_live_writes: bool = False,
    ) -> int:
        raise RuntimeError("Live Google Sheets writes are disabled in MusicVault.")
