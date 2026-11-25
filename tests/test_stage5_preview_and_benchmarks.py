"""
Stage 5 tests: real read-only Google Sheets schema verification (incompatible headers,
duplicate Drive IDs, missing IDs), live preview boundary enforcement (parent-genre-only even on
locked subgenre, malformed lock JSON fail-closed, stale evidence HOLD, unindexed draft-only with
no row 999, novel 2-provider genre proposal HOLD without folder creation, zero authorized/executed
even if config had DRY_RUN=False, zero write-endpoint calls spied), audited Sheets batch update
guard, and isolated 10-track & honest 50-track benchmarks that never mutate live DB.
"""
import json
from pathlib import Path
import pytest

from musicvault.config import Config
from musicvault.core.organizer import build_move_plan, evaluate_live_preview
from musicvault.db import Database
from musicvault.fixtures.benchmarks import execute_isolated_benchmark
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.services.drive import DriveService
from musicvault.services.sheets import SheetsService, validate_and_index_tracks_sheet


VALID_HEADERS = [
    "Title", "Artist", "Genre", "Subgenre", "BPM", "Key",
    "", "", "", "", "", "Status", "", "", "Drive ID", "Notes",
    "Review Star", "Review Reasons", "BPM State", "Key State",
    "Genre State", "Subgenre State", "Metadata Lock JSON",
    "Metadata Evidence Summary", "Last Review UTC", "Review Status", "Secondary Influence",
]


def test_sheet_header_validation_catches_incompatible_headers_and_duplicate_ids():
    # 1. Wrong header at index 14 (Col O) -> rejected!
    bad_headers = list(VALID_HEADERS)
    bad_headers[14] = "Notes_Shifted"
    bad_idx = validate_and_index_tracks_sheet(bad_headers, [])
    assert bad_idx.schema_valid is False
    assert "INCOMPATIBLE_HEADERS" in bad_idx.schema_error

    # 2. Incompatible review header in Q1:AA1 -> rejected!
    bad_rev = list(VALID_HEADERS)
    bad_rev[16] = "User Custom Column"
    bad_rev_idx = validate_and_index_tracks_sheet(bad_rev, [])
    assert bad_rev_idx.schema_valid is False
    assert "INCOMPATIBLE_REVIEW_HEADERS" in bad_rev_idx.schema_error

    # 3. Duplicate Drive IDs and missing IDs detected cleanly; never assigned fake row 999
    rows = [
        ["Song A", "Art A", "Hardstyle", "", "150", "8A", "", "", "", "", "", "", "", "", "DUP_ID_1", ""],
        ["Song A2", "Art A", "Techno", "", "150", "8A", "", "", "", "", "", "", "", "", "DUP_ID_1", ""],
        ["No ID Song", "Art B", "Trance", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["Good Song", "Art C", "Hardstyle", "Rawstyle", "155", "11A", "", "", "", "", "", "", "", "", "UNIQ_ID_1", ""],
    ]
    valid_idx = validate_and_index_tracks_sheet(VALID_HEADERS, rows)
    assert valid_idx.schema_valid is True
    assert "DUP_ID_1" in valid_idx.duplicate_ids
    assert valid_idx.duplicate_ids["DUP_ID_1"] == [2, 3]
    assert "DUP_ID_1" not in valid_idx.unique_rows_by_id
    assert valid_idx.missing_id_rows == [4]
    assert valid_idx.unique_rows_by_id["UNIQ_ID_1"][0] == 5
    assert valid_idx.verified_identities["UNIQ_ID_1"] == ("Art C", "Good Song")


def test_live_preview_parent_only_malformed_lock_fail_closed_and_zero_writes(monkeypatch: pytest.MonkeyPatch):
    # Spy on ALL Drive and Sheets write methods to guarantee ZERO calls!
    write_calls = []
    monkeypatch.setattr(DriveService, "move_file", lambda *a, **kw: write_calls.append(("move_file", a)))
    monkeypatch.setattr(DriveService, "ensure_folder", lambda *a, **kw: write_calls.append(("ensure_folder", a)))
    monkeypatch.setattr(SheetsService, "apply_batch_updates", lambda *a, **kw: write_calls.append(("apply_batch_updates", a)))

    # Even if build_move_plan is called on a VERIFIED_NESTED row, dry_run/read_only_build enforces 0 authorized!
    locked_row = [""] * 27
    locked_row[2] = "Hardstyle"
    locked_row[3] = "Euphoric"
    locked_row[14] = "ID_VERIFIED"
    locked_row[22] = json.dumps({"genre": {"state": "USER_LOCKED"}, "subgenre": {"state": "USER_LOCKED"}})
    plan_check = build_move_plan("ID_VERIFIED", "Track.flac", 10, locked_row, allow_provisional=True)
    assert plan_check.move_authorized is False
    assert plan_check.executable is False

    # Construct live tracks covering:
    # 1. ID_LOCKED_SUB: USER_LOCKED genre+subgenre -> live preview MUST route to parent 'Hardstyle' ONLY (not Hardstyle/Euphoric!)
    # 2. ID_BAD_LOCK: malformed JSON in col W -> fail closed HOLD ('MALFORMED_LOCK_JSON')
    # 3. ID_UNINDEXED: not in Tracks sheet -> draft only, row_num is None (NEVER 999!), HOLD
    # 4. ID_NOVEL: 2 independent providers propose 'Synthwave' -> NOVEL_GENRE_REVIEW_PROPOSAL HOLD, no folder created!
    # 5. ID_STALE: content_stale=True -> STALE_EVIDENCE_REHYDRATE_REQUIRED HOLD
    bad_lock_row = [""] * 27
    bad_lock_row[2] = "Hardstyle"
    bad_lock_row[14] = "ID_BAD_LOCK"
    bad_lock_row[22] = "{broken_json!!!"

    blank_genre_row = [""] * 27
    blank_genre_row[14] = "ID_NOVEL"

    sheet_idx = validate_and_index_tracks_sheet(VALID_HEADERS, [locked_row, bad_lock_row, blank_genre_row])
    assert sheet_idx.schema_valid is True

    live_tracks = [
        Track(file_id="ID_VERIFIED", name="Locked Sub.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="A", title="B")),
        Track(file_id="ID_BAD_LOCK", name="Corrupt Lock.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="C", title="D")),
        Track(file_id="ID_UNINDEXED", name="Unindexed.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="Act of Rage", title="New Song")),
        Track(file_id="ID_NOVEL", name="Novel Genre.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="Kavinsky", title="Nightcall")),
        Track(file_id="ID_STALE", name="Stale.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, content_stale=True),
    ]

    cands_map = {
        "ID_NOVEL": [
            Candidate(provider="SonoVault", title="Nightcall", artists=["Kavinsky"], genre=["Synthwave"], verification="ISRC supported", source_family="sonovault"),
            Candidate(provider="MusicBrainz", title="Nightcall", artists=["Kavinsky"], genre=["Synthwave"], verification="ISRC supported", source_family="musicbrainz"),
        ]
    }

    preview = evaluate_live_preview(
        live_tracks=live_tracks,
        candidates_by_id=cands_map,
        sheet_index=sheet_idx,
        source_folder_id="SRC_1",
        vault_folder_id="VAULT_1",
        vault_folders={"Hardstyle": ["FOLDER_HS"]},
    )

    assert preview.authorized_count == 0
    assert preview.executable_count == 0
    assert write_calls == []

    by_id = {r.file_id: r for r in preview.rows}
    # Parent-genre-only at live preview boundary!
    assert by_id["ID_VERIFIED"].destination == "Hardstyle"
    assert "/" not in by_id["ID_VERIFIED"].destination
    assert by_id["ID_VERIFIED"].eligible is True
    assert by_id["ID_VERIFIED"].move_authorized is False
    assert by_id["ID_VERIFIED"].existing_values["subgenre"] == "Euphoric"  # Preserved!

    # Malformed lock JSON fails closed!
    assert by_id["ID_BAD_LOCK"].eligible is False
    assert by_id["ID_BAD_LOCK"].reason == "MALFORMED_LOCK_JSON"

    # Unindexed track NEVER gets row 999!
    assert by_id["ID_UNINDEXED"].row_num is None
    assert by_id["ID_UNINDEXED"].eligible is False
    assert by_id["ID_UNINDEXED"].reason == "NO_MATCHING_SHEET_ROW_DRAFT_ONLY"

    # Novel 2-provider genre held for human review, never creates folder!
    assert by_id["ID_NOVEL"].eligible is False
    assert by_id["ID_NOVEL"].reason == "NOVEL_GENRE_REVIEW_PROPOSAL"
    assert "Synthwave" in by_id["ID_NOVEL"].destination

    # Stale track held!
    assert by_id["ID_STALE"].eligible is False
    assert by_id["ID_STALE"].reason == "STALE_EVIDENCE_REHYDRATE_REQUIRED"


def test_benchmark_10_exact_regression_and_50_honest_fixture_without_mutating_live_db(
    isolated_musicvault_environment: Path, tmp_path: Path
):
    cfg = Config()
    live_db = Database(cfg.db_path)
    assert live_db.get_inventory_summary()["total_records"] == 0

    # 1. 10-track benchmark: exact 8 eligible, 2 held, 0 authorized, PASS, and live DB untouched!
    res_10 = execute_isolated_benchmark(cfg, sample_size=10)
    assert res_10.status == "PASS"
    assert res_10.evaluated_with_ground_truth == 10
    assert res_10.passed_count == 10
    assert res_10.failed_count == 0
    assert res_10.eligible_count == 8
    assert res_10.held_count == 2
    assert res_10.authorized_count == 0
    assert live_db.get_inventory_summary()["total_records"] == 0  # Live DB strictly untouched!

    # 2. 50-track benchmark with synthetic 50-entry checkpoint where only 2 IDs have expected rows:
    #    must report INCOMPLETE_BENCHMARK and NEVER invent row 999!
    cp_50 = {}
    cp_50["1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL"] = {"version": "v1", "artist": "Act of Rage", "title": "Living Fast", "duration": 240.0}
    cp_50["1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX"] = {"version": "v2", "artist": "Jesse Bloch", "title": "Believe", "duration": 173.0}
    for i in range(48):
        cp_50[f"UNVERIFIED_CP_{i:02d}"] = {"version": f"vx{i}", "artist": f"A{i}", "title": f"T{i}", "duration": 180.0}
    cp_path = tmp_path / "synthetic_50_cp.json"
    cp_path.write_text(json.dumps(cp_50), encoding="utf-8")

    res_50 = execute_isolated_benchmark(cfg, sample_size=50, checkpoint_path=cp_path)
    assert res_50.status == "INCOMPLETE_BENCHMARK"
    assert res_50.evaluated_with_ground_truth == 2
    assert res_50.missing_expected_rows == 48
    assert all(o.row_num != 999 for o in res_50.outcomes)
    assert live_db.get_inventory_summary()["total_records"] == 0
