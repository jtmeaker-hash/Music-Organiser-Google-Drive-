"""
Regression tests for all independent code-review findings:
1. Tagless / filename-only audio files do not cause infinite re-hydration loops or rescan resets.
2. All-provider transient outage preserves prior valid candidates even when Wikidata fallback is enabled.
3. JSON/dict-quoted secret keys and Google '1//' OAuth refresh tokens are redacted cleanly.
4. Subgenre folders in Vault (e.g. 'Rawstyle') never collide with or hijack parent-genre routing ('Hardstyle').
5. Provider BUDGET_EXHAUSTED stops cleanly without penalizing unqueried tracks' research_attempts.
6. DriveService.ensure_folder and move_file raise RuntimeError by default in v2.0.1.
"""
import io
import json
import wave
from pathlib import Path
from typing import Optional
import pytest

from musicvault.config import redact_secrets
from musicvault.core.hydrator import hydrate_live_inventory
from musicvault.core.organizer import evaluate_live_preview
from musicvault.core.researcher import execute_research_batch
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.providers.base import ProviderCallOutcome, ProviderStatus
from musicvault.services.drive import DriveService
from musicvault.services.range_reader import ByteBudgetTracker, DriveRangeReader, RangeFetchResult
from musicvault.services.sheets import validate_and_index_tracks_sheet
from musicvault.config import Config


def _make_tagless_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 4000)
    return buf.getvalue()


def test_tagless_and_filename_only_files_do_not_rehydrate_infinitely_or_reset_on_rescan(tmp_path: Path):
    db = Database(tmp_path / "hyd_once.db")
    tagless_wav = _make_tagless_wav()

    s1 = db.begin_scan_run("SRC_1")
    db.reconcile_scanned_file(
        s1, "SRC_1",
        {"id": "TAGLESS_1", "name": "01 Clarity.wav", "size": str(len(tagless_wav)), "md5Checksum": "v_tagless", "scanned_parent": "SRC_1"},
    )
    db.reconcile_scanned_file(
        s1, "SRC_1",
        {"id": "AMBIG_1", "name": "Believe - Jesse Bloch.wav", "size": str(len(tagless_wav)), "md5Checksum": "v_ambig", "scanned_parent": "SRC_1"},
    )
    db.complete_scan_run(s1, "SRC_1", 2)

    range_opens = []

    class CountingRangeDrive:
        def open_range_reader(self, file_id: str, budget: ByteBudgetTracker, known_size: int = 0, **kwargs):
            range_opens.append(file_id)

            def _fetch(start: int, end: int, max_cap: int) -> RangeFetchResult:
                actual_end = min(end, len(tagless_wav) - 1)
                chunk = tagless_wav[start : actual_end + 1]
                budget.record(len(chunk))
                return RangeFetchResult(
                    status_code=206,
                    headers={"content-range": f"bytes {start}-{actual_end}/{len(tagless_wav)}", "etag": '"v1"'},
                    data=chunk,
                    bytes_transferred=len(chunk),
                )

            return io.BufferedReader(DriveRangeReader(_fetch, known_size=len(tagless_wav), block_size=16384), buffer_size=16384)

    # First hydration pass inspects both files
    sum1 = hydrate_live_inventory(db, CountingRangeDrive(), limit=10, max_mib=5.0)
    assert sum1.attempted == 2
    assert len(range_opens) == 2

    # Second hydration pass MUST find 0 pending tracks and make ZERO remote range requests!
    range_opens.clear()
    sum2 = hydrate_live_inventory(db, CountingRangeDrive(), limit=10, max_mib=5.0)
    assert sum2.attempted == 0
    assert range_opens == []

    # Re-scanning unchanged files MUST NOT reset already-inspected tagless/partial tracks back to 'pending'!
    s2 = db.begin_scan_run("SRC_1")
    r_tagless = db.reconcile_scanned_file(
        s2, "SRC_1",
        {"id": "TAGLESS_1", "name": "01 Clarity.wav", "size": str(len(tagless_wav)), "md5Checksum": "v_tagless", "scanned_parent": "SRC_1"},
    )
    r_ambig = db.reconcile_scanned_file(
        s2, "SRC_1",
        {"id": "AMBIG_1", "name": "Believe - Jesse Bloch.wav", "size": str(len(tagless_wav)), "md5Checksum": "v_ambig", "scanned_parent": "SRC_1"},
    )
    db.complete_scan_run(s2, "SRC_1", 2)
    assert r_tagless == "updated_preserved"
    assert r_ambig == "updated_preserved"
    assert db.get_live_tracks_for_hydration(limit=10) == []


def test_transient_outage_preserves_prior_candidates_even_when_wikidata_enabled_and_budget_pause_clean(
    isolated_musicvault_environment: Path,
):
    cfg = Config()
    db = Database(cfg.db_path)

    s1 = db.begin_scan_run("SRC_1")
    db.reconcile_scanned_file(s1, "SRC_1", {"id": "TR_0", "name": "Act of Rage - Song 0.flac", "md5Checksum": "v0", "scanned_parent": "SRC_1"})
    db.update_hydration_result(
        "TR_0",
        status="success",
        version="v0",
        method="embedded_ranged",
        metadata=TrackMetadata(version="v0", artist="Act of Rage", title="Song 0", duration=240.0, identity_origin="embedded", identity_confidence="verified_embedded"),
    )
    db.complete_scan_run(s1, "SRC_1", 1)

    # Seed valid recording-verified candidate on TR_0
    db.update_research_result(
        "TR_0",
        status="success",
        version="v0",
        candidates=[Candidate(provider="MusicBrainz", title="Song 0", artists=["Act of Rage"], duration=240.0, genre=["Hardstyle"], verification="ISRC supported", source_family="musicbrainz")],
        provider_calls={"MusicBrainz": {"status": "ok"}},
    )

    class OutageMB:
        stats = {"calls": 1}
        def can_request(self) -> bool:
            return True
        def search_detailed(self, artist: str, title: str, isrc: Optional[str] = None):
            return [], ProviderCallOutcome(provider="MusicBrainz", status=ProviderStatus.SERVER_ERROR, http_status=503, error="503 outage", retry_after_seconds=60, request_made=True)

    class WikiWouldReturnHint:
        stats = {"calls": 0}
        def research_track(self, artist: str, title: str):
            self.stats["calls"] += 1
            return {"genre_hints": [{"url": "https://wikidata.org/wiki/Q1", "parent_genres": ["Hardstyle"]}]}

    wiki_spy = WikiWouldReturnHint()
    # Run with enable_wikidata_fallback=True (default!) during MB 503 outage on TR_0:
    # Wikidata must NOT run, TR_0 must become retryable_failure, and prior MB candidate must be preserved!
    sum_out = execute_research_batch(
        cfg,
        db,
        limit=1,
        force_refresh=True,
        enable_wikidata_fallback=True,
        providers_override={"mb": OutageMB(), "wiki": wiki_spy},
    )
    assert sum_out.failed_retryable == 1
    assert wiki_spy.stats["calls"] == 0
    preserved = db.get_candidates("TR_0")
    assert len(preserved) == 1
    assert preserved[0].provider == "MusicBrainz"

    # Add pending TR_1 and test BUDGET_EXHAUSTED (request_made=False): must pause cleanly without incrementing TR_1's research_attempts!
    s2 = db.begin_scan_run("SRC_1")
    db.reconcile_scanned_file(s2, "SRC_1", {"id": "TR_0", "name": "Act of Rage - Song 0.flac", "md5Checksum": "v0", "scanned_parent": "SRC_1"})
    db.reconcile_scanned_file(s2, "SRC_1", {"id": "TR_1", "name": "Act of Rage - Song 1.flac", "md5Checksum": "v1", "scanned_parent": "SRC_1"})
    db.update_hydration_result(
        "TR_1",
        status="success",
        version="v1",
        method="embedded_ranged",
        metadata=TrackMetadata(version="v1", artist="Act of Rage", title="Song 1", duration=240.0, identity_origin="embedded", identity_confidence="verified_embedded"),
    )
    db.complete_scan_run(s2, "SRC_1", 2)
    class ExhaustedMB:
        stats = {"calls": 0}
        def can_request(self) -> bool:
            return False
        def search_detailed(self, artist: str, title: str, isrc: Optional[str] = None):
            return [], ProviderCallOutcome(provider="MusicBrainz", status=ProviderStatus.BUDGET_EXHAUSTED, error="cap reached", request_made=False)

    sum_bud = execute_research_batch(cfg, db, limit=2, providers_override={"mb": ExhaustedMB()})
    assert sum_bud.paused_provider_budget is True
    assert sum_bud.attempted == 0
    tr1 = db.get_track("TR_1")
    assert tr1 is not None and tr1.research_attempts == 0 and tr1.research_status == "pending"


def test_redact_secrets_handles_quoted_json_dict_keys_and_google_refresh_tokens():
    sample = (
        '{"refresh_token": "1//04abcdef1234567890XYZ", "client_secret": "my_json_secret", '
        '"x-api-key": "sv_quoted_999", "token": "ya29.secret_tok_abc"} '
        "and {'api_key': 'py_dict_secret_val'} and standalone 1//09standaloneRefreshToken999"
    )
    clean = redact_secrets(sample)
    for leaked in (
        "1//04abcdef1234567890XYZ",
        "my_json_secret",
        "sv_quoted_999",
        "ya29.secret_tok_abc",
        "py_dict_secret_val",
        "1//09standaloneRefreshToken999",
    ):
        assert leaked not in clean
    assert "<REDACTED" in clean


def test_subgenre_folder_in_vault_never_hijacks_or_blocks_parent_genre_routing():
    headers = [
        "Title", "Artist", "Genre", "Subgenre", "BPM", "Key",
        "", "", "", "", "", "", "", "", "Drive ID", "Notes",
    ]
    rows = [
        ["Living Fast", "Act of Rage", "Hardstyle", "Rawstyle", "158", "11A", "", "", "", "", "", "", "", "", "TR_HS", ""],
        ["Big City Life", "Luude", "Drum & Bass", "", "174", "5B", "", "", "", "", "", "", "", "", "TR_DNB", ""],
    ]
    idx = validate_and_index_tracks_sheet(headers, rows)
    tracks = [
        Track(file_id="TR_HS", name="Living Fast.flac", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="Act of Rage", title="Living Fast")),
        Track(file_id="TR_DNB", name="Big City Life.wav", parent_id="SRC_1", observed_parent_id="SRC_1", is_live_source=True, metadata=TrackMetadata(artist="Luude", title="Big City Life")),
    ]

    # Case 1: Vault has BOTH 'Hardstyle' and 'Rawstyle' folders + 'Drum and Bass' spelling variant!
    prev = evaluate_live_preview(
        live_tracks=tracks,
        candidates_by_id={},
        sheet_index=idx,
        source_folder_id="SRC_1",
        vault_folder_id="VAULT_1",
        vault_folders={"Hardstyle": ["F_HS"], "Rawstyle": ["F_RAW"], "Drum and Bass": ["F_DNB"]},
    )
    by_id = {r.file_id: r for r in prev.rows}
    assert by_id["TR_HS"].eligible is True
    assert by_id["TR_HS"].destination == "Hardstyle"  # Not blocked as ambiguous by 'Rawstyle'!
    assert by_id["TR_DNB"].eligible is True
    assert by_id["TR_DNB"].destination == "Drum and Bass"  # Matched parent spelling alias!

    # Case 2: Vault has ONLY 'Rawstyle' (no 'Hardstyle' parent folder) -> must propose 'Hardstyle', NEVER route into 'Rawstyle'!
    prev2 = evaluate_live_preview(
        live_tracks=[tracks[0]],
        candidates_by_id={},
        sheet_index=idx,
        source_folder_id="SRC_1",
        vault_folder_id="VAULT_1",
        vault_folders={"Rawstyle": ["F_RAW"]},
    )
    assert prev2.rows[0].destination == "Hardstyle"
    assert prev2.rows[0].folder_action == "PROPOSE_CREATE_PARENT_FOLDER"


def test_drive_write_methods_raise_by_default_in_v201():
    # Instantiate without calling googleapiclient build()
    drv = DriveService.__new__(DriveService)
    with pytest.raises(RuntimeError, match="disabled in Music Vault v2.0.1"):
        drv.ensure_folder("PARENT_1", "Hardstyle")
    with pytest.raises(RuntimeError, match="disabled in Music Vault v2.0.1"):
        drv.move_file("FILE_1", "NEW_P", "OLD_P")
