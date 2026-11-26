"""
Comprehensive regression suite for MusicVault v2.0.3 full remediation.
Verifies all 12 independent audit findings, approved PROVIDER_TERMS_CLEARED=False and
SONOVAULT_MODE=fallback policies, interrupted folder-level Drive scan resumption without
duplicate list calls, quota reset track unblocking, and 2,268-track streaming memory bounds.
"""
import json
import sqlite3
import tracemalloc
from argparse import Namespace
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from musicvault.__main__ import main as cli_main
from musicvault.config import Config
from musicvault.core.coverage import decisions, library_report
from musicvault.core.inventory import scan_root
from musicvault.core.organizer import evaluate_live_preview
from musicvault.core.process import run_process
from musicvault.core.provider_policy import ProviderPolicy, provider_report
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.providers.audiometa import AudioMetaProvider
from musicvault.providers.base import BaseProvider, ProviderCallOutcome, ProviderStatus
from musicvault.providers.discogs import DiscogsProvider
from musicvault.providers.musicbrainz import MusicBrainzProvider
from musicvault.providers.sonovault import SonoVaultProvider
from musicvault.services.drive import DriveService
from musicvault.services.sheets import validate_and_index_tracks_sheet


def _proc_args(**overrides):
    base = {
        "scope": "all",
        "limit": 0,
        "batch_size": 10,
        "cooldown": 0,
        "concurrency": 1,
        "resume": False,
        "inventory_only": False,
        "dry_run": False,
        "file_id": [],
        "provider": "",
        "verbose": True,
        "export_json": "",
        "export_csv": "",
    }
    base.update(overrides)
    return Namespace(**base)


def _http_resp(body='{"recordings": []}', code=200, retry_after=None):
    r = requests.Response()
    r.status_code = code
    r._content = body.encode("utf-8")
    if retry_after is not None:
        r.headers["Retry-After"] = str(retry_after)
    return r


def test_remediation_1_request_monitor_count_and_server_deadlines(tmp_path, monkeypatch):
    db = Database(tmp_path / "library.db")
    cfg = Config(tmp_path)
    policy = ProviderPolicy(cfg)
    now = datetime.now(timezone.utc)
    tick = 0.0

    mb = BaseProvider(
        "MusicBrainz",
        db=db,
        default_delay=0.0,
        max_retry_sleep_seconds=0.0,
        clock_mono=lambda: tick,
        clock_utc=lambda: now + timedelta(seconds=tick),
        sleep_fn=lambda _d: None,
    )
    mb.ALLOWS_OPEN_PERSISTENT_CACHE = True
    policy.bind(db, {"mb": mb})

    # 1. Cache populate + cache hit must NOT raise NameError and must record cache_hits metric
    monkeypatch.setattr(mb.session, "get", lambda *a, **kw: _http_resp())
    assert mb.request_detailed("https://musicbrainz.example/rec", params={"q": "1"}, use_cache=True).status == ProviderStatus.OK
    cached_out = mb.request_detailed("https://musicbrainz.example/rec", params={"q": "1"}, use_cache=True)
    assert cached_out.status == ProviderStatus.OK and cached_out.from_cache is True

    # 2. Server 429 with Retry-After=120 records provider_server_deadlines and deadline_source == 'server',
    #    and subsequent cooldown skip records cooldown_skips without raising NameError!
    monkeypatch.setattr(mb.session, "get", lambda *a, **kw: _http_resp(body="{}", code=429, retry_after=120))
    rl_out = mb.request_detailed("https://musicbrainz.example/rec", params={"q": "rl"}, use_cache=False)
    assert rl_out.status == ProviderStatus.RATE_LIMITED
    skip_out = mb.request_detailed("https://musicbrainz.example/rec", params={"q": "rl2"}, use_cache=False)
    assert skip_out.status == ProviderStatus.COOLDOWN_DEFERRED

    with closing(db._get_connection()) as conn:
        rep = provider_report(conn, cfg)
        assert rep["musicbrainz"]["cache_hits"] == 1
        assert rep["musicbrainz"]["cooldown_skips"] == 1
        assert rep["musicbrainz"]["deadline_source"] == "server"

    # 3. Advance past cooldown: malformed JSON records malformed_responses without NameError,
    #    and clean 200 OK clears provider_server_deadlines!
    tick += 125.0
    monkeypatch.setattr(mb.session, "get", lambda *a, **kw: _http_resp(body="{bad_json", code=200))
    mal_out = mb.request_detailed("https://musicbrainz.example/rec", params={"q": "bad"}, use_cache=False)
    assert mal_out.status == ProviderStatus.MALFORMED_JSON

    tick += 20.0
    monkeypatch.setattr(mb.session, "get", lambda *a, **kw: _http_resp(body="{}", code=200))
    assert mb.request_detailed("https://musicbrainz.example/rec", params={"q": "clean"}, use_cache=False).status == ProviderStatus.OK
    with closing(db._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_server_deadlines WHERE provider='musicbrainz'").fetchone()[0] == 0
        rep2 = provider_report(conn, cfg)
        assert rep2["musicbrainz"]["malformed_responses"] == 1


def test_remediation_2_sonovault_fallback_essential_fields_and_cooldown_deferral(tmp_path, monkeypatch):
    monkeypatch.setenv("SONOVAULT_MODE", "fallback")
    cfg = Config(tmp_path)
    cfg.set("SONOVAULT_API_KEY", "sv_key_test")
    cfg.set("AUDIOMETA_API_KEY", "am_key_test")
    cfg.set("DISCOGS_TOKEN", "dc_key_test")
    db = Database(cfg.db_path)

    class TwoTrackDrive:
        def scan_inventory(self, root):
            if root != cfg.source_folder_id:
                return
                yield
            yield {"id": "t_resolved", "name": "Act of Rage - Living Fast.flac", "parents": [root], "md5Checksum": "v1"}
            yield {"id": "t_am_cd", "name": "Rebelion - City Lights.flac", "parents": [root], "md5Checksum": "v2"}

    drive = TwoTrackDrive()
    run_process(cfg, db, drive, _proc_args(scope="source", inventory_only=True))
    db.update_hydration_result(
        "t_resolved", "success", "v1", method="embedded_ranged",
        metadata=TrackMetadata(version="v1", artist="Act of Rage", title="Living Fast", duration=210.0, isrc="NLFL72401034", identity_origin="embedded"),
    )
    db.update_hydration_result(
        "t_am_cd", "success", "v2", method="embedded_ranged",
        metadata=TrackMetadata(version="v2", artist="Rebelion", title="City Lights", duration=200.0, isrc="NLFL72409999", identity_origin="embedded"),
    )

    calls = []

    class StubMB:
        source_family = "musicbrainz"
        request_monitor = None
        stats = {"calls": 0}
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return True
        def search_detailed(self, artist, title, isrc=""):
            calls.append(("mb", title))
            return [
                Candidate("MusicBrainz", title, [artist], duration=210.0 if title == "Living Fast" else 200.0,
                          isrc=isrc, genre=["hardstyle", "rawstyle"], verification="ISRC supported", source_family="musicbrainz")
            ], ProviderCallOutcome("MusicBrainz", ProviderStatus.OK, request_made=True)

    class StubDC:
        source_family = "discogs"
        request_monitor = None
        stats = {"calls": 0}
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return True
        def search_detailed(self, artist, title):
            calls.append(("dc", title))
            # Note: single label only on Discogs, subgenre corroborated case-insensitively with MB 'rawstyle'
            return [
                Candidate("Discogs release", title, [artist], duration=210.0 if title == "Living Fast" else 200.0,
                          genre=["Hardstyle"], labels=["Minus Is More"], verification="Duration supported", source_family="discogs")
            ], ProviderCallOutcome("Discogs", ProviderStatus.OK, request_made=True)

    am_state = {"mode": "ok_first_then_429"}

    class StubAM:
        source_family = "audiometa"
        request_monitor = None
        stats = {"calls": 0}
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return am_state["mode"] != "cooling"
        def search_detailed(self, artist, title):
            calls.append(("am", title))
            if title == "Living Fast":
                # Single-provider BPM + Key (MEDIUM) resolves essential BPM/Key -> must NOT trigger SonoVault!
                return [
                    Candidate("Audiometa", title, [artist], duration=210.0, isrc="NLFL72401034", bpm=155.0, key="11A",
                              verification="ISRC supported", source_family="audiometa")
                ], ProviderCallOutcome("AudioMeta", ProviderStatus.OK, request_made=True)
            if am_state["mode"] == "ok_first_then_429":
                am_state["mode"] = "cooling"
                return [], ProviderCallOutcome(
                    "AudioMeta", ProviderStatus.RATE_LIMITED, http_status=429, retry_after_seconds=90.0,
                    retry_until_utc=(datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(), request_made=True,
                )
            return [], ProviderCallOutcome("AudioMeta", ProviderStatus.EMPTY, request_made=True)

    class StubSono:
        source_family = "sonovault"
        request_monitor = None
        stats = {"calls": 0}
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return True
        def search_detailed(self, artist, title, isrc=""):
            calls.append(("sono", title))
            return [
                Candidate("SonoVault", title, [artist], duration=200.0, isrc=isrc, bpm=152.0, key="8A", genre=["Hardstyle"],
                          verification="ISRC supported", source_family="sonovault")
            ], ProviderCallOutcome("SonoVault", ProviderStatus.OK, request_made=True)

    provs = {"mb": StubMB(), "dc": StubDC(), "am": StubAM(), "sono": StubSono()}
    res1 = run_process(cfg, db, drive, _proc_args(scope="source"), policy=ProviderPolicy(cfg), providers_override=provs)

    # Pass 1 assertions:
    # - Track 1 ('Living Fast'): MB + DC + AM resolved identity, genre, BPM, key (missing subgenre/2nd label did NOT trigger SonoVault!)
    # - Track 2 ('City Lights'): AM hit 429 cooldown -> SonoVault DEFERRED ('deferred_primary_incomplete'), ZERO SonoVault credits spent!
    assert not any(p == "sono" for p, _ in calls)
    t2_audit = json.loads(db.get_track("t_am_cd").provider_calls_json)
    assert t2_audit["SonoVault"]["status"] == "deferred_primary_incomplete"
    assert res1["status"] == "paused"

    # Pass 2 (AudioMeta cooldown expires and returns EMPTY on 'City Lights'):
    # - MB and DC are NOT re-queried for 'City Lights' (0 duplicate calls!)
    # - AudioMeta completes with EMPTY -> SonoVault NOW triggers cleanly for 'City Lights' ONLY!
    calls.clear()
    am_state["mode"] = "recovered_empty"
    with closing(db._get_connection()) as conn, conn:
        conn.execute("UPDATE tracks SET research_next_retry_utc='' WHERE file_id='t_am_cd'")
        conn.execute("UPDATE enrichment_work SET due_utc='' WHERE file_id='t_am_cd'")

    res2 = run_process(cfg, db, drive, _proc_args(scope="source", resume=True), policy=ProviderPolicy(cfg), providers_override=provs)
    assert res2["status"] == "completed"
    assert calls == [("am", "City Lights"), ("sono", "City Lights")]

    # Direct post-audit edge-case verification on fallback_needed_fields:
    pol_direct = ProviderPolicy(cfg)
    pol_direct.bind(db, {})
    dummy_t = Track("edge_1", "A - B.flac", metadata=TrackMetadata(artist="A", title="B", duration=180.0, version="v1"))
    # (a) Single-provider genre (MB ok + DC empty) + AM BPM/key -> still needs 2nd genre family for HIGH routing!
    cands_single_genre = [
        Candidate("MusicBrainz", "B", ["A"], duration=180.0, genre=["hardstyle"], verification="Duration supported", source_family="musicbrainz"),
        Candidate("Audiometa", "B", ["A"], duration=180.0, bpm=150.0, key="8A", verification="Duration supported", source_family="audiometa"),
    ]
    assert pol_direct.fallback_needed_fields(dummy_t, cands_single_genre) == {"genre"}
    # (b) Unresolvable primary genre conflict (MB Hardstyle vs DC Techno) + AM BPM/key -> must NOT waste SonoVault credit!
    cands_conflict_genre = cands_single_genre + [
        Candidate("Discogs release", "B", ["A"], duration=180.0, genre=["Techno"], verification="Duration supported", source_family="discogs"),
    ]
    assert pol_direct.fallback_needed_fields(dummy_t, cands_conflict_genre) == set()
    assert pol_direct.reason_to_skip("sonovault", pol_direct.fallback_needed_fields(dummy_t, cands_conflict_genre)) == "sufficient_evidence"


def test_remediation_3_process_batch_budget_replenishment_exceeds_250_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRED_RESEARCH_PROVIDERS", "musicbrainz")
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)
    with closing(db._get_connection()) as wconn:
        wconn.execute("PRAGMA journal_mode=WAL")

    class SixtyFiveTrackDrive:
        def scan_inventory(self, root):
            for i in range(65):
                yield {"id": f"trk_{i:03d}", "name": f"Artist - Song {i:03d}.flac", "parents": [root], "md5Checksum": f"v{i}"}

    drive = SixtyFiveTrackDrive()
    run_process(cfg, db, drive, _proc_args(scope="source", inventory_only=True))
    for i in range(65):
        db.update_hydration_result(
            f"trk_{i:03d}", "success", f"v{i}", method="embedded_ranged",
            metadata=TrackMetadata(version=f"v{i}", artist="Artist", title=f"Song {i:03d}", duration=180.0, identity_origin="embedded"),
        )

    mb = MusicBrainzProvider(
        db=db,
        default_delay=0.0,
        clock_mono=lambda: 1000.0,
        clock_utc=lambda: datetime.now(timezone.utc),
        sleep_fn=lambda _d: None,
    )
    http_total = 0

    def _four_calls_per_track(url, params=None, **kw):
        nonlocal http_total
        http_total += 1
        if url.endswith("/recording"):
            q = (params or {}).get("query", "")
            # Extract song title to synthesize 3 recording hits -> 1 search + 3 detail = 4 HTTP calls/track (260 total > 250!)
            song = q.split('recording:"')[1].split('"')[0]
            return _http_resp(json.dumps({
                "recordings": [
                    {"id": f"{song}-{k}", "title": song, "length": 180000, "artist-credit": [{"name": "Artist"}]}
                    for k in range(3)
                ]
            }))
        return _http_resp(json.dumps({"id": "rec", "title": "Song", "length": 180000, "genres": [{"name": "hardstyle", "count": 2}]}))

    monkeypatch.setattr(mb.session, "get", _four_calls_per_track)
    res = run_process(
        cfg, db, drive, _proc_args(scope="source", limit=0, batch_size=10),
        policy=ProviderPolicy(cfg), providers_override={"mb": mb},
    )
    assert http_total == 260  # Exceeded old 250 cap cleanly via per-track/batch budget replenishment!
    assert res["status"] == "completed"
    assert res["enrichment_pending"] == 0


def test_remediation_4_enrichment_work_caps_retries_at_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRED_RESEARCH_PROVIDERS", "musicbrainz")
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    class SingleTrackDrive:
        def scan_inventory(self, root):
            yield {"id": "poison_id", "name": "Artist - Poison.flac", "parents": [root], "md5Checksum": "v1"}
        def get_children_folders(self, root):
            return {}

    class EmptySheet:
        def fetch_header_row(self, *a):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID"]
        def iter_tracks_rows(self, *a):
            return
            yield

    class FailingMB:
        source_family = "musicbrainz"
        request_monitor = None
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return True
        def search_detailed(self, artist, title, isrc=""):
            return [], ProviderCallOutcome("MusicBrainz", ProviderStatus.SERVER_ERROR, http_status=500, request_made=True)

    drive = SingleTrackDrive()
    run_process(cfg, db, drive, _proc_args(scope="source", inventory_only=True))
    db.update_hydration_result(
        "poison_id", "success", "v1", method="embedded_ranged",
        metadata=TrackMetadata(version="v1", artist="Artist", title="Poison", duration=180.0, identity_origin="embedded"),
    )

    last_res = None
    for i in range(5):
        last_res = run_process(
            cfg, db, drive, _proc_args(scope="source", resume=(i > 0)),
            policy=ProviderPolicy(cfg), providers_override={"mb": FailingMB()},
        )
        with closing(db._get_connection()) as conn, conn:
            conn.execute("UPDATE tracks SET research_next_retry_utc='' WHERE file_id='poison_id'")
            conn.execute("UPDATE enrichment_work SET due_utc='' WHERE file_id='poison_id'")
        if last_res["status"] == "completed":
            break

    tr = db.get_track("poison_id")
    assert tr.research_attempts == 3  # Capped at MAX_ENRICHMENT_ATTEMPTS=3, no infinite loop!
    assert last_res is not None and last_res["status"] == "completed"
    assert last_res["enrichment_pending"] == 0

    exp = tmp_path / "poison_cov.json"
    with closing(db._get_connection()) as conn:
        library_report(conn, db, cfg, _proc_args(export_json=str(exp)), EmptySheet(), drive)
    row = json.loads(exp.read_text())["rows"][0]
    assert row["states"]["enrichment_pending"] is False
    assert row["states"]["review_required"] is True
    assert row["states"]["workflow_complete"] is True


def test_remediation_5_terms_cleared_false_in_session_visibility_cache_gate_and_terms_hold(tmp_path, monkeypatch):
    cfg = Config(tmp_path)  # PROVIDER_TERMS_CLEARED defaults to False
    assert cfg.provider_terms_cleared is False
    cfg.set("SONOVAULT_API_KEY", "sv_tok")
    cfg.set("AUDIOMETA_API_KEY", "am_tok")
    cfg.set("DISCOGS_TOKEN", "dc_tok")
    db = Database(cfg.db_path)

    class OneTrackDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                yield {"id": "terms_t1", "name": "Artist - TermsTrack.flac", "parents": [root], "scanned_parent": root, "md5Checksum": "v1"}
            else:
                return
                yield
        def get_children_folders(self, root):
            return {"Hardstyle": ["HS_FOLDER"]}

    class BlankRowSheet:
        def fetch_header_row(self, *a):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID"]
        def iter_tracks_rows(self, *a):
            yield 2, ["TermsTrack", "Artist", "", "", "", ""] + [""] * 8 + ["terms_t1"]

    drive = OneTrackDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")
    db.update_hydration_result(
        "terms_t1", "success", "v1", method="embedded_ranged",
        metadata=TrackMetadata(version="v1", artist="Artist", title="TermsTrack", duration=200.0, identity_origin="embedded"),
    )

    sono = SonoVaultProvider(api_key="sv_tok", db=db, provider_terms_cleared=False, default_delay=0.0)
    am = AudioMetaProvider(api_key="am_tok", db=db, provider_terms_cleared=False, default_delay=0.0)
    monkeypatch.setattr(sono.session, "get", lambda *a, **kw: _http_resp(json.dumps({
        "results": [{"title": "TermsTrack", "artists": [{"name": "Artist"}], "duration": 200, "bpm": 150, "key_alphanumeric": "8A", "genre": "Hardstyle"}]
    })))
    monkeypatch.setattr(am.session, "get", lambda *a, **kw: _http_resp(json.dumps({
        "tracks": [{"id": "1", "name": "TermsTrack", "artists": [{"name": "Artist"}], "duration_ms": 200000, "audio_features": {"tempo": 150.0, "key": 9, "mode": 0}}]
    })))
    sono_cands, _ = sono.search_detailed("Artist", "TermsTrack")
    am_cands, _ = am.search_detailed("Artist", "TermsTrack")
    for c in sono_cands + am_cands:
        c.verification = "Duration supported"
    db.update_research_result(
        "terms_t1", "success", "v1", sono_cands + am_cands,
        {"_meta": {"lookup_complete": True}, "SonoVault": {"request_made": True}}, research_incomplete=False,
    )

    with closing(db._get_connection()) as conn:
        # 0 restricted payloads written to provider_cache!
        assert conn.execute("SELECT COUNT(*) FROM provider_cache WHERE provider IN ('sonovault','audiometa')").fetchone()[0] == 0
        exp = tmp_path / "terms_cov.json"
        rep = library_report(conn, db, cfg, _proc_args(export_json=str(exp)), BlankRowSheet(), drive)

    # Candidates ARE visible in coverage, while spreadsheet proposed cell is held under TERMS_HOLD!
    assert rep["fields"]["source"]["bpm"]["verified"] == 1
    assert rep["fields"]["source"]["bpm"]["terms_hold"] == 1
    row = json.loads(exp.read_text())["rows"][0]
    assert row["fields"]["bpm"]["value"] == "150.0"
    assert row["fields"]["bpm"]["proposed"] == ""
    assert row["fields"]["bpm"]["terms_hold"] is True

    # Also verify DISABLED_RESEARCH_PROVIDERS hides disabled family cleanly
    monkeypatch.setenv("DISABLED_RESEARCH_PROVIDERS", "sonovault")
    with closing(db._get_connection()) as conn:
        rep_dis = library_report(conn, db, cfg, _proc_args(), BlankRowSheet(), drive)
    assert rep_dis["providers"]["sonovault"]["availability"] == "DISABLED"
    assert rep_dis["fields"]["source"]["bpm"]["candidate_only"] == 1


def test_remediation_6_overlapping_roots_multiparent_version_change_and_standalone_preview_hold(tmp_path):
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    state = {"md5": "v1"}

    class OverlapAndMultiParentDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                yield {
                    "id": "ovl_track",
                    "name": "Artist - Overlap.flac",
                    "parents": [root, "SECOND_PARENT"],
                    "scanned_parent": root,
                    "md5Checksum": state["md5"],
                }
            else:
                yield {
                    "id": "ovl_track",
                    "name": "Artist - Overlap.flac",
                    "parents": ["VAULT_SUB"],
                    "scanned_parent": "VAULT_SUB",
                    "md5Checksum": state["md5"],
                }

    drive = OverlapAndMultiParentDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")

    tr = db.get_track("ovl_track")
    assert tr.is_live_source is True
    assert tr.parent_id == cfg.source_folder_id  # Preserved! Not overwritten by VAULT_SUB!
    assert tr.observed_parent_id == cfg.source_folder_id
    assert tr.location_ambiguous is True

    # Standalone `evaluate_live_preview` MUST hold overlapping track!
    headers = ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID"]
    rows = [["Overlap", "Artist", "Hardstyle", "", "150", "8A"] + [""] * 8 + ["ovl_track"]]
    sheet_idx = validate_and_index_tracks_sheet(headers, rows)
    prev = evaluate_live_preview(
        live_tracks=db.get_all_tracks(live_only=True),
        candidates_by_id={},
        sheet_index=sheet_idx,
        source_folder_id=cfg.source_folder_id,
        vault_folder_id=cfg.vault_folder_id,
        vault_folders={"Hardstyle": ["VAULT_HS"]},
    )
    assert prev.rows[0].eligible is False
    assert prev.rows[0].reason == "AMBIGUOUS_OVERLAPPING_ROOT_MEMBERSHIP"

    # Seed HIGH_LOCKED (system-derived) vs USER_LOCKED (manual) in field_decisions, then change MD5 on multi-parent track:
    with closing(db._get_connection()) as conn, conn:
        conn.execute("INSERT INTO field_decisions VALUES ('ovl_track','bpm','150','HIGH','HIGH_LOCKED','auto','2026-01-01')")
        conn.execute("INSERT INTO field_decisions VALUES ('ovl_track','genre','Hardstyle','HIGH','USER_LOCKED','manual','2026-01-01')")

    state["md5"] = "v2_replaced_audio"
    scan_root(db, drive, cfg.source_folder_id, "source")
    tr_v2 = db.get_track("ovl_track")
    assert tr_v2.metadata.version == "v2_replaced_audio"
    assert tr_v2.content_stale is True
    with closing(db._get_connection()) as conn:
        kept_decs = dict(conn.execute("SELECT field_name, state FROM field_decisions WHERE file_id='ovl_track'").fetchall())
    assert kept_decs == {"genre": "USER_LOCKED"}  # Stale HIGH_LOCKED cleared & archived; manual USER_LOCKED preserved!


def test_remediation_7_coverage_preview_ready_multilabel_and_duration_tolerance(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TERMS_CLEARED", "true")
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    class ReadyDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                yield {"id": "rdy_1", "name": "Headhunterz & Sub Zero Project - Our Church.flac", "parents": [root], "scanned_parent": root, "md5Checksum": "v1"}
            else:
                return
                yield
        def get_children_folders(self, root):
            return {"Hardstyle": ["VAULT_HS"]}

    class ReadySheet:
        def fetch_header_row(self, *a):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID"]
        def iter_tracks_rows(self, *a):
            yield 2, ["Our Church", "Headhunterz & Sub Zero Project", "", "", "", ""] + [""] * 8 + ["rdy_1"]

    drive = ReadyDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")
    db.update_hydration_result(
        "rdy_1", "success", "v1", method="filename_hint",
        metadata=TrackMetadata(
            version="v1", artist="Headhunterz & Sub Zero Project", title="Our Church",
            duration=0.0, identity_origin="filename_hint", identity_confidence="filename_ambiguous",
        ),
    )
    db.update_research_result(
        "rdy_1",
        status="success",
        version="v1",
        candidates=[
            Candidate("MusicBrainz", "Our Church", ["Headhunterz", "Sub Zero Project"], duration=180.13,
                      genre=["hardstyle"], labels=["Art of Creation"], verification="Duration supported", source_family="musicbrainz"),
            Candidate("Discogs release", "Our Church", ["Headhunterz & Sub Zero Project"], duration=180.0,
                      genre=["Hardstyle"], labels=["Art Of Creation", "Scantraxx"], verification="Duration supported", source_family="discogs"),
        ],
        provider_calls={"_meta": {"lookup_complete": True}, "MusicBrainz": {"request_made": True}},
        research_incomplete=False,
    )
    exp = tmp_path / "rdy_cov.json"
    with closing(db._get_connection()) as conn:
        library_report(conn, db, cfg, _proc_args(export_json=str(exp)), ReadySheet(), drive)
    row = json.loads(exp.read_text())["rows"][0]
    assert row["fields"]["artist"]["state"] == "verified"
    assert row["fields"]["duration"]["state"] == "verified"
    assert row["fields"]["label"]["state"] == "verified"
    assert row["fields"]["label"]["conflict"] is False
    assert row["states"]["metadata_complete"] is False  # BPM/Key/Subgenre/ISRC still missing
    assert row["routing"] == "preview_ready"  # Parent-genre routing ready without requiring 10-field completeness!
    assert row["destination"] == "Hardstyle"


def test_remediation_8_v1_to_v3_seeds_memberships_source_change_clears_ghosts_and_delete_mode_migrates(tmp_path):
    from tests.test_stage2_db_migration import _build_legacy_v200_db

    v1_path = tmp_path / "v1_direct.db"
    _build_legacy_v200_db(v1_path)
    db_v1 = Database(v1_path)
    with closing(db_v1._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM root_membership WHERE role='source'").fetchone()[0] == 858

    # Simulate changing SOURCE_FOLDER_ID from 'SRC_ROOT' to 'NEW_SRC_ROOT' and scanning:
    class NewSourceDrive:
        def scan_inventory(self, root):
            yield {"id": "NEW_LIVE_1", "name": "New - Track.flac", "parents": [root], "scanned_parent": root, "md5Checksum": "vn1"}

    scan_root(db_v1, NewSourceDrive(), "NEW_SRC_ROOT", "source")
    inv = db_v1.get_inventory_summary()
    assert inv["live_source_tracks"] == 1  # Old 858 SRC_ROOT tracks cleanly marked non-live!
    assert inv["source_membership_tracks"] == 1

    # Also verify non-WAL (DELETE journal mode) v2.0.2 -> v3 migration
    v2_path = tmp_path / "v2_delete.db"
    schema_sql = (Path(__file__).parent / "fixtures" / "musicvault_v202_schema.sql").read_text()
    with closing(sqlite3.connect(v2_path)) as c2:
        c2.executescript(schema_sql)
        assert c2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        c2.execute(
            "INSERT INTO tracks(file_id,name,version,is_live_source,configured_source_id,observed_parent_id) VALUES ('id1','a.flac','v1',1,'SRC','SRC')"
        )
        c2.commit()
    db_v2 = Database(v2_path)
    assert db_v2.get_schema_version() == 3
    assert db_v2.get_inventory_summary()["source_membership_tracks"] == 1


def test_remediation_9_cooldown_deferred_tracks_do_not_consume_process_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRED_RESEARCH_PROVIDERS", "musicbrainz,audiometa")
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    class TwoTrackDrive:
        def scan_inventory(self, root):
            yield {"id": "t_defer", "name": "A - One.flac", "parents": [root], "md5Checksum": "v1"}
            yield {"id": "t_ready", "name": "B - Two.flac", "parents": [root], "md5Checksum": "v1"}

    mb_called = []

    class HealthyMB:
        source_family = "musicbrainz"
        request_monitor = None
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return True
        def search_detailed(self, a, t, isrc=""):
            mb_called.append(t)
            return [], ProviderCallOutcome("MusicBrainz", ProviderStatus.EMPTY, request_made=True)

    class CoolingAM:
        source_family = "audiometa"
        request_monitor = None
        def _now_utc_normalized(self): return datetime.now(timezone.utc)
        def can_request(self): return False
        def search_detailed(self, a, t):
            return [], ProviderCallOutcome(
                "AudioMeta", ProviderStatus.COOLDOWN_DEFERRED, retry_after_seconds=60,
                retry_until_utc=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                deferred_by_cooldown=True, request_made=False,
            )

    drive = TwoTrackDrive()
    run_process(cfg, db, drive, _proc_args(scope="source", inventory_only=True))
    for fid, ti in (("t_defer", "One"), ("t_ready", "Two")):
        db.update_hydration_result(
            fid, "success", "v1", method="embedded_ranged",
            metadata=TrackMetadata(version="v1", artist="Artist", title=ti, duration=180.0, identity_origin="embedded"),
        )
    db.update_research_result(
        "t_defer", "retryable_failure", "v1", [],
        {
            "MusicBrainz": {"status": "empty", "lookup_completed": True, "query_artist": "Artist", "query_title": "One", "query_isrc": ""},
            "AudioMeta": {"status": "rate_limited", "lookup_completed": False, "retryable": True},
        },
        research_incomplete=True, consume_attempt=False,
    )
    with closing(db._get_connection()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO scheduler_state VALUES ('first_pass', 4)")

    res = run_process(
        cfg, db, drive, _proc_args(scope="source", limit=1),
        policy=ProviderPolicy(cfg), providers_override={"mb": HealthyMB(), "am": CoolingAM()},
    )
    assert mb_called == ["Two"]  # t_defer did NOT steal the --limit 1 slot; t_ready executed on MusicBrainz!
    assert res["research_selected"] == 1


def test_remediation_10_nested_vault_subfolders_destination_status_and_destination_recovery(tmp_path):
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    class NestedVaultDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                return
                yield
            yield {
                "id": "vault_nested_1",
                "name": "Artist - Nested.flac",
                "parents": ["SUB_RAWSTYLE_ID"],
                "scanned_parent": "SUB_RAWSTYLE_ID",
                "scanned_path": ["Hardstyle", "Rawstyle", "Artist - Nested.flac"],
                "md5Checksum": "v1",
            }
        def get_children_folders(self, root):
            return {"Hardstyle": ["PARENT_HARDSTYLE_ID"]}

    class EmptySheet:
        def fetch_header_row(self, *a):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID"]
        def iter_tracks_rows(self, *a):
            return
            yield

    drive = NestedVaultDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")
    db.update_hydration_result(
        "vault_nested_1", "success", "v1", method="embedded_ranged",
        metadata=TrackMetadata(version="v1", artist="Artist", title="Nested", duration=200.0, identity_origin="embedded"),
    )
    db.update_research_result(
        "vault_nested_1",
        status="success",
        version="v1",
        candidates=[
            Candidate("MusicBrainz", "Nested", ["Artist"], duration=200.0, genre=["Hardstyle"], verification="Duration supported", source_family="musicbrainz"),
            Candidate("Discogs release", "Nested", ["Artist"], duration=200.0, genre=["Hardstyle"], verification="Duration supported", source_family="discogs"),
        ],
        provider_calls={"_meta": {"lookup_complete": True}, "AudioMeta": {"status": "rate_limited", "lookup_completed": False, "retryable": True}},
        research_incomplete=False,
    )

    exp = tmp_path / "nested_cov.json"
    with closing(db._get_connection()) as conn:
        library_report(conn, db, cfg, _proc_args(export_json=str(exp)), EmptySheet(), drive)
    saved = json.loads(exp.read_text())
    assert saved["rows"][0]["routing"] == "already_correctly_located"

    inv = db.get_inventory_summary()
    assert inv["destination_tracks"] == 1
    assert inv["unique_library_tracks"] == 1

    rec = db.inspect_or_apply_research_recovery(configured_providers={"musicbrainz", "audiometa"}, apply=True, create_backup=True)
    assert rec["counts"]["recoverable_total"] == 1
    assert rec["counts"]["applied_count"] == 1
    assert db.get_track("vault_nested_1").research_incomplete is True


def test_remediation_11_clear_quota_reopens_blocked_tracks_and_resets_breakers(isolated_musicvault_environment):
    cfg = Config()
    db = Database(cfg.db_path)
    db.save_track(
        Track(
            file_id="quota_t1",
            name="Artist - QuotaSong.flac",
            is_live_source=True,
            hydration_status="success",
            hydration_version="v1",
            research_status="unresolved",
            research_version="v1",
            provider_calls_json=json.dumps({"SonoVault": {"status": "quota_exceeded", "lookup_completed": False}}),
            metadata=TrackMetadata(version="v1", artist="Artist", title="QuotaSong", duration=180.0, identity_origin="embedded"),
        )
    )
    db.set_provider_operational_state("sonovault", last_status="quota_exceeded", cooldown_until_utc="")
    with closing(db._get_connection()) as conn, conn:
        conn.execute("INSERT INTO provider_breakers VALUES ('sonovault', '[true]', 3, 2, '2099-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO provider_server_deadlines VALUES ('sonovault', '2099-01-01T00:00:00+00:00')")

    rc = cli_main(["clear-quota", "sonovault", "--confirm-reset"])
    assert rc == 0
    tr = db.get_track("quota_t1")
    assert tr.research_status == "retryable_failure"
    assert tr.research_incomplete is True
    assert db.get_live_tracks_for_research(10) != []
    with closing(db._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_breakers WHERE provider='sonovault'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provider_server_deadlines WHERE provider='sonovault'").fetchone()[0] == 0


def test_remediation_12_interrupted_drive_scan_resumes_without_duplicate_folder_lists_and_2268_memory(tmp_path):
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    # 12a. Interrupted multi-folder DriveService scan resumes at unfinished folder without re-listing completed folders
    listed_folders = []
    interrupt_once = {"armed": True}

    class ResumableAPI:
        def files(self):
            return self
        def get(self, **kwargs):
            class Req:
                def execute(self, **_kw):
                    return {"id": kwargs["fileId"], "mimeType": "application/vnd.google-apps.folder", "driveId": "shared_1"}
            return Req()
        def list(self, **kwargs):
            q = kwargs["q"]
            listed_folders.append(q)
            class Req:
                def execute(self, **_kw):
                    if f"'{cfg.source_folder_id}'" in q:
                        return {"files": [
                            {"id": "sub_A", "name": "SubA", "mimeType": "application/vnd.google-apps.folder"},
                            {"id": "sub_B", "name": "SubB", "mimeType": "application/vnd.google-apps.folder"},
                        ]}
                    if "'sub_A'" in q:
                        return {"files": [{"id": "file_A1", "name": "A1.flac", "parents": ["sub_A"], "md5Checksum": "ma1"}]}
                    if "'sub_B'" in q:
                        if interrupt_once["armed"]:
                            interrupt_once["armed"] = False
                            raise KeyboardInterrupt
                        return {"files": [{"id": "file_B1", "name": "B1.flac", "parents": ["sub_B"], "md5Checksum": "mb1"}]}
                    return {"files": []}
            return Req()

    svc = DriveService.__new__(DriveService)
    svc.service = ResumableAPI()

    r1 = run_process(cfg, db, svc, _proc_args(scope="source", inventory_only=True))
    assert r1["status"] == "interrupted"
    # Before full root completion, atomic publication kept root_membership empty:
    with closing(db._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM root_membership").fetchone()[0] == 0

    listed_folders.clear()
    r2 = run_process(cfg, db, svc, _proc_args(scope="source", inventory_only=True, resume=True))
    assert r2["status"] == "completed"
    # Root and sub_A were NOT re-requested on resume; ONLY sub_B was listed!
    assert len(listed_folders) == 1 and "'sub_B'" in listed_folders[0]
    assert r2["inventory"]["source"] == 2

    # Also verify shared-drive get_children_folders passes driveId corpus
    children = svc.get_children_folders(cfg.source_folder_id)
    assert set(children) == {"SubA", "SubB"}

    # 12b. Full 2,268-track streaming inventory + JSON/CSV preview memory bound (< 32 MiB)
    cfg_mem = Config(tmp_path / "mem")
    db_mem = Database(cfg_mem.db_path)

    class Synthetic2268Drive:
        def scan_inventory(self, root):
            cnt = 881 if root == cfg_mem.source_folder_id else 1387
            pfx = "src" if root == cfg_mem.source_folder_id else "dst"
            for i in range(cnt):
                yield {"id": f"{pfx}_{i:04d}_id", "name": f"Artist {i} - Track {i}.flac", "parents": [root], "scanned_parent": root, "md5Checksum": f"m_{pfx}_{i}"}
        def get_children_folders(self, root):
            return {"Hardstyle": ["HS_ID"]}

    class Synthetic2268Sheet:
        def fetch_header_row(self, *a):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM (full)", "Key"] + [""] * 8 + ["Drive ID"]
        def iter_tracks_rows(self, *a):
            for i in range(881):
                yield i + 2, [f"Track {i}", f"Artist {i}", "Hardstyle", "", "150", "8A"] + [""] * 8 + [f"src_{i:04d}_id"]

    drv_mem = Synthetic2268Drive()
    tracemalloc.start()
    scan_root(db_mem, drv_mem, cfg_mem.source_folder_id, "source")
    scan_root(db_mem, drv_mem, cfg_mem.vault_folder_id, "destination")
    with closing(db_mem._get_connection()) as conn:
        rep_mem = library_report(
            conn, db_mem, cfg_mem,
            _proc_args(export_json=str(tmp_path / "m.json"), export_csv=str(tmp_path / "m.csv")),
            Synthetic2268Sheet(), drv_mem,
        )
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert rep_mem["tracks"]["source"]["denominator"] == 881
    assert rep_mem["tracks"]["destination"]["denominator"] == 1387
    assert rep_mem["tracks"]["unique"]["denominator"] == 2268
    assert peak_bytes < 32 * 1024 * 1024


def test_remediation_13_cli_pre_migration_status_backup_and_explicit_migrate_workflow(
    isolated_musicvault_environment, capsys
):
    """Rehearse the exact documented Termux upgrade sequence on a schema-v2 database."""
    cfg = Config()
    schema_sql = (Path(__file__).parent / "fixtures" / "musicvault_v202_schema.sql").read_text()
    with closing(sqlite3.connect(cfg.db_path)) as conn:
        conn.executescript(schema_sql)
        conn.execute(
            """
            INSERT INTO tracks(file_id,name,version,is_live_source,configured_source_id,observed_parent_id,artist,title)
            VALUES ('pre_mig_1','Artist - KeepMe.flac','v1',1,?,?, 'Artist', 'KeepMe')
            """,
            (cfg.source_folder_id, cfg.source_folder_id),
        )
        conn.commit()

    # 1. Pre-upgrade status & backup work on schema v2 without auto-migrating
    assert cli_main(["status"]) == 0
    assert Database(cfg.db_path, initialize=False).get_schema_version() == 2
    assert cli_main(["backup", "--label", "pre_upgrade_rehearsal"]) == 0

    # 2. Normal processing before migration fails closed with actionable message
    assert cli_main(["process", "--inventory-only"]) == 2
    err = capsys.readouterr().err
    assert "migrate --confirm-stopped" in err

    # 3. Explicit migration upgrades to schema v3, creates verified backup, and seeds membership
    assert cli_main(["migrate", "--confirm-stopped"]) == 0
    migrated = Database(cfg.db_path, initialize=False)
    assert migrated.get_schema_version() == 3
    inv = migrated.get_inventory_summary()
    assert inv["live_source_tracks"] == 1
    assert inv["source_membership_tracks"] == 1

