"""
Google Sheets v4 API client for reading master library index, verifying locks, and safe updates.
"""
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timezone
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from ..core.rules import EXTRA_HEADERS, FIELDS, COL


class SheetsService:
    """Wrapper around Google Sheets v4 API."""

    def __init__(self, credentials: Credentials):
        self.service = build('sheets', 'v4', credentials=credentials)

    def fetch_master_rows(self, spreadsheet_id: str, range_name: str = "Tracks!A2:AA") -> List[List[Any]]:
        """Fetch all track rows from master spreadsheet."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()
        return resp.get('values', [])

    def fetch_row(self, spreadsheet_id: str, row_num: int) -> List[Any]:
        """Fetch a single row (columns A:AA)."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"Tracks!A{row_num}:AA{row_num}"
        ).execute()
        vals = resp.get('values', [[]])
        return vals[0] if vals else []

    def verify_headers(self, spreadsheet_id: str) -> bool:
        """Verify that review headers in Q1:AA1 are empty or match expected schema."""
        resp = self.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Tracks!Q1:AA1"
        ).execute()
        header = resp.get('values', [[]])[0]
        header = header + [''] * (len(EXTRA_HEADERS) - len(header))
        return not any(v and v != expected for v, expected in zip(header, EXTRA_HEADERS))

    def create_pre_write_backup(self, spreadsheet_id: str, rows_to_backup: List[Tuple[int, List[Any]]],
                                backup_dir: Path) -> Path:
        """Create a local JSON backup of original rows before any update."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"music_vault_backup_{timestamp}.json"
        data = {
            'spreadsheet_id': spreadsheet_id,
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'rows': [{'row': r_num, 'values': vals} for r_num, vals in rows_to_backup]
        }
        with backup_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return backup_path

    def apply_batch_updates(self, spreadsheet_id: str, updates: List[Tuple[int, List[Any], List[Any]]]) -> int:
        """
        Execute safe batch updates:
        1. Checks headers
        2. Detects concurrent edits
        3. Writes empty field cells and review columns Q:AA
        """
        if not self.verify_headers(spreadsheet_id):
            raise RuntimeError("Headers in Q1:AA1 do not match expected review schema.")

        # Re-verify rows immediately before writing to catch concurrent edits
        for row_num, old_vals, _ in updates:
            current = self.fetch_row(spreadsheet_id, row_num)
            curr_padded = (list(current) + [''] * 27)[:27]
            old_padded = (list(old_vals) + [''] * 27)[:27]
            if curr_padded != old_padded:
                raise RuntimeError(f"Concurrent sheet edit detected at row {row_num}; aborting write.")

        requests_to_send = [
            {'range': 'Tracks!Q1:AA1', 'values': [EXTRA_HEADERS]}
        ]

        col_letters = {2: 'C', 3: 'D', 4: 'E', 5: 'F'}

        for row_num, old_vals, new_vals in updates:
            # Write individual newly resolved fields without touching existing values
            for field in FIELDS:
                col_idx = COL[field]
                has_old = len(old_vals) > col_idx and bool(old_vals[col_idx])
                has_new = len(new_vals) > col_idx and bool(new_vals[col_idx])
                if not has_old and has_new:
                    col_letter = col_letters[col_idx]
                    requests_to_send.append({
                        'range': f"Tracks!{col_letter}{row_num}",
                        'values': [[new_vals[col_idx]]]
                    })
            # Write review metadata columns Q:AA (indices 16 to 26)
            requests_to_send.append({
                'range': f"Tracks!Q{row_num}:AA{row_num}",
                'values': [new_vals[16:27]]
            })

        if requests_to_send:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'valueInputOption': 'RAW', 'data': requests_to_send}
            ).execute()

        return len(requests_to_send)
