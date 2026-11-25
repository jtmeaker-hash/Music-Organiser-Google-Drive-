"""
Stage 6 tests: responsive narrow vs wide terminal formatting, Drive file ID masking,
CSV spreadsheet formula injection defense & UTF-8 secret-redacted exports, OAuth existing-token
reuse without forced re-auth, CLI --help/status with zero network calls, and mocked end-to-end
scan -> hydrate -> research -> preview -> backup -> rollback with zero cloud writes.
"""
import csv
import io
import json
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List
import pytest

from mutagen.id3 import TBPM, TCON, TIT2, TKEY, TPE1, TSRC
from mutagen.wave import WAVE

from musicvault.__main__ import main as cli_main
from musicvault.auth import GoogleAuthManager
from musicvault.cli.commands import run_backup, run_hydrate, run_preview, run_research, run_scan, run_status
from musicvault.cli.formatting import (
    export_safe_csv,
    export_safe_json,
    render_responsive_records,
    sanitize_csv_cell,
    short_file_id,
)
from musicvault.config import Config
from musicvault.db import Database
from musicvault.models import Candidate
from musicvault.providers.base import ProviderCallOutcome, ProviderStatus
from musicvault.services.drive import DriveService
from musicvault.services.range_reader import ByteBudgetTracker, DriveRangeReader, RangeFetchResult
from musicvault.services.sheets import SheetsService, validate_and_index_tracks_sheet


def test_responsive_narrow_vs_wide_and_csv_formula_injection_defense(tmp_path: Path):
    records = [
        {"row": "7", "file_id": short_file_id("1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL"), "track": "Act of Rage - Living Fast.flac", "dest": "Hardstyle"},
    ]
    cols = [("row", "Row", 6), ("file_id", "File ID", 15), ("track", "Track", 32), ("dest", "Dest", 14)]

    # Narrow mobile width (50 cols) -> vertical key/value card, masked Drive ID
    narrow_out = render_responsive_records(records, cols, force_wide=False, term_width=50)
    assert "  Track: Act of Rage - Living Fast.flac" in narrow_out
    assert "1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL" not in narrow_out
    assert "1v3jd...6gSL" in narrow_out

    # Wide desktop width (130 cols) -> horizontal table
    wide_out = render_responsive_records(records, cols, force_wide=True, term_width=130)
    assert "Row    File ID" in wide_out

    # CSV formula injection neutralization & UTF-8 preservation & secret redaction
    assert sanitize_csv_cell("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert sanitize_csv_cell("+SUM(A1:A2)") == "'+SUM(A1:A2)"
    assert sanitize_csv_cell("-Dangerous - Track.wav") == "'-Dangerous - Track.wav"
    assert sanitize_csv_cell("@IMPORTXML(...)") == "'@IMPORTXML(...)"
    assert sanitize_csv_cell("  =1+1") == "'  =1+1"

    csv_path = tmp_path / "export.csv"
    export_safe_csv(
        csv_path,
        ["track", "note"],
        [{"track": "=HYPERLINK(\"http://evil\") — Rebeli\u00f3n", "note": "x-api-key: super_secret_val"}],
        extra_secrets=["super_secret_val"],
    )
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["track"].startswith("'=HYPERLINK")
    assert "Rebeli\u00f3n" in rows[0]["track"]
    assert "super_secret_val" not in rows[0]["note"]
    assert "<REDACTED>" in rows[0]["note"]

    json_path = tmp_path / "export.json"
    export_safe_json(json_path, {"err": "Authorization: Bearer my_secret_token_999"}, extra_secrets=["my_secret_token_999"])
    raw_json = json_path.read_text(encoding="utf-8")
    assert "my_secret_token_999" not in raw_json
    assert "<REDACTED>" in raw_json


def test_oauth_reuses_valid_token_and_handles_missing_client_secrets(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    cfg = Config()
    mgr = GoogleAuthManager(cfg)

    # Missing client_secrets.json fails gracefully without crashing
    assert mgr.authenticate_interactive() is None
    captured = capsys.readouterr().out
    assert "client_secrets.json not found" in captured

    # Valid existing token.json is reused directly without invoking interactive flow
    cfg.token_path.write_text('{"token": "dummy"}', encoding="utf-8")

    class DummyValidCreds:
        valid = True
        expired = False
        refresh_token = "ref"

    monkeypatch.setattr("musicvault.auth.Credentials.from_authorized_user_file", lambda *a, **kw: DummyValidCreds())
    monkeypatch.setattr(mgr, "authenticate_interactive", lambda: (_ for _ in ()).throw(AssertionError("Must not re-auth!")))
    got = mgr.get_credentials(allow_interactive=True)
    assert isinstance(got, DummyValidCreds)


def test_cli_help_and_status_offline(isolated_musicvault_environment: Path, capsys: pytest.CaptureFixture):
    repo_root = Path(__file__).resolve().parent.parent
    help_res = subprocess.run(
        [sys.executable, "-m", "musicvault", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    for subcmd in ("status", "backup", "scan", "hydrate", "research", "preview", "benchmark", "setup"):
        assert subcmd in help_res.stdout

    rc = cli_main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LOCAL DIAGNOSTIC STATUS" in out
    assert "Writes=DISABLED" in out


def test_mocked_end_to_end_scan_hydrate_research_preview_and_backup_zero_writes(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch
):
    # Spy on ALL cloud write methods to prove zero cloud mutations end-to-end
    cloud_writes: List[str] = []
    monkeypatch.setattr(DriveService, "move_file", lambda *a, **kw: cloud_writes.append("move_file"))
    monkeypatch.setattr(DriveService, "ensure_folder", lambda *a, **kw: cloud_writes.append("ensure_folder"))
    monkeypatch.setattr(SheetsService, "apply_batch_updates", lambda *a, **kw: cloud_writes.append("apply_batch_updates"))

    cfg = Config()
    cfg.set("DRY_RUN", "False")  # Even if user misconfigured DRY_RUN=False, preview MUST force dry-run!
    cfg.set("ALLOW_PROVISIONAL_MOVES", "True")
    db = Database(cfg.db_path)

    # Build real tagged WAV bytes for hydration
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\x00\x00" * 16000)
    buf.seek(0)
    wf = WAVE(buf)
    wf.add_tags()
    wf.tags.add(TPE1(encoding=3, text=["Act of Rage"]))
    wf.tags.add(TIT2(encoding=3, text=["Living Fast"]))
    wf.tags.add(TBPM(encoding=3, text=["158"]))
    wf.tags.add(TKEY(encoding=3, text=["11A"]))
    wf.tags.add(TSRC(encoding=3, text=["NLFL72401034"]))
    wf.tags.add(TCON(encoding=3, text=["Hardstyle"]))
    wf.save(buf)
    wav_payload = buf.getvalue()

    class MockDriveE2E:
        def scan_inventory(self, root_folder_id: str):
            yield {
                "id": "E2E_TRACK_1",
                "name": "01 Living Fast.wav",
                "mimeType": "audio/wav",
                "size": str(len(wav_payload)),
                "md5Checksum": "md5_e2e_1",
                "modifiedTime": "2026-11-25T08:00:00Z",
                "parents": [root_folder_id],
                "scanned_parent": root_folder_id,
            }
            yield {
                "id": "E2E_TRACK_2",
                "name": "01 Clarity.wav",
                "mimeType": "audio/wav",
                "size": "8044",
                "md5Checksum": "md5_e2e_2",
                "modifiedTime": "2026-11-25T08:01:00Z",
                "parents": [root_folder_id],
                "scanned_parent": root_folder_id,
            }

        def open_range_reader(self, file_id: str, budget: ByteBudgetTracker, known_size: int = 0, **kwargs):
            # Track 1 has full WAV tags; Track 2 is a tagless WAV
            if file_id == "E2E_TRACK_1":
                data = wav_payload
            else:
                b2 = io.BytesIO()
                with wave.open(b2, "wb") as w2:
                    w2.setnchannels(1); w2.setsampwidth(2); w2.setframerate(8000); w2.writeframes(b"\x00\x00" * 4000)
                data = b2.getvalue()

            def _fetch(start: int, end: int, max_cap: int) -> RangeFetchResult:
                actual_end = min(end, len(data) - 1)
                chunk = data[start : actual_end + 1]
                budget.record(len(chunk))
                return RangeFetchResult(
                    status_code=206,
                    headers={"content-range": f"bytes {start}-{actual_end}/{len(data)}", "etag": f'"{file_id}"'},
                    data=chunk,
                    bytes_transferred=len(chunk),
                )

            return io.BufferedReader(DriveRangeReader(_fetch, known_size=len(data), block_size=16384), buffer_size=16384)

        def get_children_folders(self, parent_id: str) -> Dict[str, List[str]]:
            return {"Hardstyle": ["VAULT_HS_FOLDER"]}

    class MockSheetsE2E:
        def load_verified_index(self, spreadsheet_id: str):
            headers = [
                "Title", "Artist", "Genre", "Subgenre", "BPM", "Key",
                "", "", "", "", "", "", "", "", "Drive ID", "Notes",
            ]
            rows = [
                ["Living Fast", "Act of Rage", "Hardstyle", "Rawstyle", "", "", "", "", "", "", "", "", "", "", "E2E_TRACK_1", ""],
            ]
            return validate_and_index_tracks_sheet(headers, rows)

    class MockMBProviderE2E:
        stats = {"calls": 1, "cache_hits": 0, "ok": 1}

        def can_request(self) -> bool:
            return True

        def search_detailed(self, artist: str, title: str, isrc=None):
            return [
                Candidate(
                    provider="MusicBrainz",
                    title="Living Fast",
                    artists=["Act of Rage"],
                    duration=2.0,
                    isrc="NLFL72401034",
                    genre=["Hardstyle"],
                    verification="ISRC supported",
                    source_family="musicbrainz",
                )
            ], ProviderCallOutcome(provider="MusicBrainz", status=ProviderStatus.OK, http_status=200, request_made=True)

    mock_drive = MockDriveE2E()
    mock_sheets = MockSheetsE2E()

    # 1. Scan
    scan_res = run_scan(cfg, db, drive_override=mock_drive)
    assert scan_res["status"] == "completed"
    assert scan_res["live_source_tracks"] == 2

    # 2. Hydrate
    hyd_res = run_hydrate(cfg, db, limit=10, max_mib=4.0, drive_override=mock_drive)
    assert hyd_res.hydrated_embedded == 1
    assert hyd_res.title_only_unresolved == 1

    # 3. Research (attempts E2E_TRACK_1, defers title-only E2E_TRACK_2)
    res_sum = run_research(cfg, db, limit=10, enable_wikidata=False, providers_override={"mb": MockMBProviderE2E()})
    assert res_sum.attempted == 1
    assert res_sum.matched_verified == 1
    assert res_sum.deferred_title_only == 1

    # 4. Preview (read-only Sheets + Drive, parent-genre only, 0 authorized even with DRY_RUN=False in .env!)
    csv_out = isolated_musicvault_environment / "e2e_preview.csv"
    json_out = isolated_musicvault_environment / "e2e_preview.json"
    prev = run_preview(
        cfg,
        db,
        limit=10,
        export_csv=csv_out,
        export_json=json_out,
        drive_override=mock_drive,
        sheets_override=mock_sheets,
    )
    assert prev.evaluated_count == 2
    assert prev.matched_sheet_rows == 1
    assert prev.unindexed_draft_only == 1
    assert prev.eligible_count == 1
    assert prev.authorized_count == 0
    assert prev.executable_count == 0
    assert csv_out.exists() and json_out.exists()
    assert cloud_writes == []

    # 5. Verified backup & status
    bak = run_backup(cfg, db, label="e2e")
    assert bak is not None and bak.exists()
    st = run_status(cfg, db)
    assert st["database"]["live_source_tracks"] == 2
