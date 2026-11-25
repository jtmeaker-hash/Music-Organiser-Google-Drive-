"""
Stage 4 tests: research queue scheduling (skips missing identity without consuming --limit,
advances across batches without repeating, honest no-work report), provider HTTP status & wizard
verification (401/403, 429 integer & HTTP-date Retry-After, quota, malformed JSON, 5xx, 404),
terms-aware cache retention, recording verification (unrelated same-duration blocked, remix/version
mismatch blocked), independent source-family provenance, Wikidata schema safety, old candidate
retention on transient outage, and evidence-to-decision wiring.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
import requests

from musicvault.cli.wizard import format_provider_probe_status
from musicvault.config import Config
from musicvault.core.matcher import verify_recording_candidate
from musicvault.core.researcher import execute_research_batch
from musicvault.core.rules import build_track_proposal, field_decision, planned_row
from musicvault.db import Database
from musicvault.models import Candidate, TrackMetadata
from musicvault.providers.audiometa import AudioMetaProvider
from musicvault.providers.base import ProviderCallOutcome, ProviderStatus, parse_retry_after
from musicvault.providers.discogs import DiscogsProvider
from musicvault.providers.musicbrainz import MusicBrainzProvider
from musicvault.providers.research import ResearchProvider
from musicvault.providers.sonovault import SonoVaultProvider


class FakeHTTPResponse:
    def __init__(self, status_code: int, json_data: Any = None, text: str = "", headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if (json_data is not None and not isinstance(json_data, Exception)) else "")
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


def test_provider_http_status_wizard_verification_and_terms_cache_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "prov_cache.db")

    # 1. Auth error (HTTP 401) must NOT report verified in wizard!
    sono_bad = SonoVaultProvider(api_key="invalid_secret_key_xyz", db=db, provider_terms_cleared=False, default_delay=0.0)
    monkeypatch.setattr(sono_bad.session, "get", lambda *a, **kw: FakeHTTPResponse(401, {"error": "Unauthorized"}))
    out_401 = sono_bad.verify_connectivity()
    assert out_401.status == ProviderStatus.AUTH_ERROR
    msg_401 = format_provider_probe_status(out_401)
    assert "Authentication/permission failed" in msg_401
    assert "invalid_secret_key_xyz" not in out_401.error

    # 2. Malformed JSON -> MALFORMED_JSON
    am_bad = AudioMetaProvider(api_key="amsk_test123456", db=db, default_delay=0.0)
    monkeypatch.setattr(am_bad.session, "get", lambda *a, **kw: FakeHTTPResponse(200, ValueError("Not JSON")))
    out_mal = am_bad.verify_connectivity()
    assert out_mal.status == ProviderStatus.MALFORMED_JSON

    # 3. Unsupported endpoint (404) and Quota exceeded (429 monthly quota)
    dc = DiscogsProvider(token="dc_tok_secret999", db=db, default_delay=0.0)
    monkeypatch.setattr(dc.session, "get", lambda *a, **kw: FakeHTTPResponse(404, {"message": "Not Found"}))
    assert dc.verify_connectivity().status == ProviderStatus.UNSUPPORTED_ENDPOINT

    sono_quota = SonoVaultProvider(api_key="sv_key_123456", db=db, default_delay=0.0)
    monkeypatch.setattr(
        sono_quota.session,
        "get",
        lambda *a, **kw: FakeHTTPResponse(429, {"error": "Monthly plan quota exceeded"}, headers={"Retry-After": "60"}),
    )
    assert sono_quota.verify_connectivity().status == ProviderStatus.QUOTA_EXCEEDED

    # 4. Retry-After parses both integer seconds and HTTP-date
    assert parse_retry_after("7") == 7.0
    http_date_delay = parse_retry_after("Wed, 25 Nov 2099 10:00:00 GMT", max_cap=90.0)
    assert http_date_delay == 90.0

    # 5. Restricted provider (SonoVault) does NOT persist to SQLite cache when PROVIDER_TERMS_CLEARED=False,
    #    whereas MusicBrainz (CC0) DOES persist to SQLite cache!
    sono_ok = SonoVaultProvider(api_key="sv_valid_key_999", db=db, provider_terms_cleared=False, default_delay=0.0)
    monkeypatch.setattr(
        sono_ok.session,
        "get",
        lambda *a, **kw: FakeHTTPResponse(
            200,
            {"results": [{"title": "Living Fast", "artists": [{"name": "Act of Rage"}], "bpm": 158, "key_alphanumeric": "11A", "duration": 240, "genre": "Hardstyle"}]},
        ),
    )
    cands_sv = sono_ok.search("Act of Rage", "Living Fast")
    assert len(cands_sv) == 1
    assert cands_sv[0].genre == ["Hardstyle"]  # Handled string genre safely!
    with db._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_cache WHERE provider='sonovault'").fetchone()[0] == 0

    mb_ok = MusicBrainzProvider(contact="test@example.com", db=db, provider_terms_cleared=False, default_delay=0.0)
    monkeypatch.setattr(
        mb_ok.session,
        "get",
        lambda url, **kw: FakeHTTPResponse(
            200,
            {"recordings": [{"id": "rec-1", "title": "Living Fast", "length": 240000, "artist-credit": [{"name": "Act of Rage"}]}]}
            if url.endswith("/recording")
            else {"id": "rec-1", "title": "Living Fast", "length": 240000, "isrcs": ["NLFL72401034"], "genres": [{"name": "hardstyle", "count": 3}], "tags": []},
        ),
    )
    cands_mb = mb_ok.search("Act of Rage", "Living Fast")
    assert len(cands_mb) == 1
    with db._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_cache WHERE provider='musicbrainz'").fetchone()[0] >= 1


def test_recording_verification_blocks_unrelated_same_duration_and_remix_mismatch():
    # 1. Unrelated track with exact same 200.0s duration MUST NOT become 'Duration supported'!
    v_unrel = verify_recording_candidate(
        query_artist="Act of Rage",
        query_title="Living Fast",
        identity_confidence="verified_embedded",
        local_isrc="",
        local_duration=200.0,
        candidate_title="Completely Different Song",
        candidate_artists=["Someone Else"],
        candidate_isrc="",
        candidate_duration=200.0,
    )
    assert v_unrel == "Identity mismatch"

    # 2. Original vs Darren Styles Remix MUST block verification even if duration happens to be close!
    v_remix = verify_recording_candidate(
        query_artist=" Darren Styles ",
        query_title="Beneath The Lights",
        identity_confidence="verified_embedded",
        local_isrc="",
        local_duration=210.0,
        candidate_title="Beneath The Lights (Darren Styles Remix)",
        candidate_artists=["Darren Styles"],
        candidate_isrc="",
        candidate_duration=210.0,
    )
    assert v_remix == "Version conflict"

    # 3. Title-only track NEVER auto-verifies even with matching duration
    v_title_only = verify_recording_candidate(
        query_artist="",
        query_title="Clarity",
        identity_confidence="title_only",
        local_isrc="",
        local_duration=271.0,
        candidate_title="Clarity",
        candidate_artists=["Zedd"],
        candidate_isrc="",
        candidate_duration=271.0,
    )
    assert v_title_only == "Title-only review"


def test_independent_source_provenance_wikidata_schema_and_decision_pipeline(monkeypatch: pytest.MonkeyPatch):
    # Two candidates from the SAME source family (e.g. two MusicBrainz recordings) must be MEDIUM, NOT HIGH!
    same_src = [
        Candidate(provider="MusicBrainz", title="Amen", artists=["Dexperia"], bpm=180.0, key="7B", genre=["Drum & Bass"], verification="ISRC supported"),
        Candidate(provider="MusicBrainz", title="Amen", artists=["Dexperia"], bpm=180.0, key="7B", genre=["Drum & Bass"], verification="Duration supported"),
    ]
    assert field_decision(same_src, "bpm", 180.0)[0] == "MEDIUM"
    assert field_decision(same_src, "genre", "Drum & Bass")[0] == "MEDIUM"

    # Two genuinely independent providers agreeing -> HIGH!
    indep_src = [
        Candidate(provider="SonoVault", title="Amen", artists=["Dexperia"], bpm=180.0, key="7B", genre=["Drum & Bass"], verification="ISRC supported"),
        Candidate(provider="MusicBrainz", title="Amen", artists=["Dexperia"], bpm=180.0, key="7B", genre=["dnb"], verification="ISRC supported"),
    ]
    meta = TrackMetadata(artist="Dexperia", title="Amen", duration=169.45, embedded_bpm="60", embedded_key="7B")
    prop = build_track_proposal(meta, indep_src)
    # Provider recording-supported BPM (180.0) overrides unverified half-time embedded tag (60)!
    assert prop["bpm"] == 180.0
    assert prop["key"] == "7B"
    assert prop["genre"] == "Drum & Bass"
    assert field_decision(indep_src, "bpm", prop)[0] == "HIGH"
    assert field_decision(indep_src, "genre", prop)[0] == "HIGH"

    # With PROVIDER_TERMS_CLEARED=False (default), planned_row holds writes under TERMS_HOLD
    old_blank = [""] * 27
    new_r, dec_r, chg_r = planned_row(indep_src, prop, old_blank, provider_terms_cleared=False)
    assert dec_r["bpm"] == "TERMS_HOLD"
    assert dec_r["genre"] == "TERMS_HOLD"
    assert chg_r == []

    # Wikidata schema check: ignores non-music 'House of Representatives' and returns clean parent strings
    wiki = ResearchProvider(default_delay=0.0)
    monkeypatch.setattr(
        wiki.session,
        "get",
        lambda *a, **kw: FakeHTTPResponse(
            200,
            {
                "search": [
                    {"id": "Q1", "label": "Speaker", "description": "member of the House of Representatives"},
                    {"id": "Q2", "label": "Act of Rage", "description": "Dutch hardstyle DJ and music producer"},
                ]
            },
        ),
    )
    res = wiki.research_track("Act of Rage", "Unknown")
    assert len(res["genre_hints"]) == 1
    assert res["genre_hints"][0]["parent_genres"] == ["Hardstyle"]


def test_research_batch_advances_skips_missing_identity_and_retains_candidates_on_outage(
    isolated_musicvault_environment: Path,
):
    cfg = Config()
    db = Database(cfg.db_path)

    # Seed 5 missing-identity tracks (alphabetically '00_...') + 4 ready tracks + 1 quarantined checkpoint track
    s1 = db.begin_scan_run("SRC_1")
    for i in range(5):
        db.reconcile_scanned_file(s1, "SRC_1", {"id": f"MISS_{i}", "name": f"00_Missing_{i}.wav", "md5Checksum": f"m{i}", "scanned_parent": "SRC_1"})
    for i in range(4):
        fid = f"READY_{i}"
        db.reconcile_scanned_file(s1, "SRC_1", {"id": fid, "name": f"Artist {i} - Song {i}.flac", "md5Checksum": f"r{i}", "scanned_parent": "SRC_1"})
        db.update_hydration_result(
            fid,
            status="success",
            version=f"r{i}",
            method="embedded_ranged",
            metadata=TrackMetadata(version=f"r{i}", artist=f"Artist {i}", title=f"Song {i}", duration=200.0, identity_origin="embedded", identity_confidence="verified_embedded"),
        )
    db.complete_scan_run(s1, "SRC_1", 9)

    class StubMBProvider:
        def __init__(self, fail_transient: bool = False):
            self.fail_transient = fail_transient
            self.queried: List[str] = []
            self.stats = {"calls": 0}

        def can_request(self) -> bool:
            return True

        def search_detailed(self, artist: str, title: str, isrc: Optional[str] = None):
            self.queried.append(title)
            self.stats["calls"] += 1
            if self.fail_transient:
                return [], ProviderCallOutcome(provider="MusicBrainz", status=ProviderStatus.SERVER_ERROR, http_status=503, error="503 outage", retry_after_seconds=60, request_made=True)
            return [
                Candidate(provider="MusicBrainz", title=title, artists=[artist], duration=200.0, genre=["Hardstyle"], verification="Duration supported", source_family="musicbrainz")
            ], ProviderCallOutcome(provider="MusicBrainz", status=ProviderStatus.OK, http_status=200, request_made=True)

    # Batch 1 (--limit 2): must skip the 5 '00_Missing' tracks without wasting --limit, and process READY_0, READY_1!
    mb1 = StubMBProvider()
    sum1 = execute_research_batch(cfg, db, limit=2, enable_wikidata_fallback=False, providers_override={"mb": mb1})
    assert sum1.deferred_missing_identity == 5
    assert sum1.attempted == 2
    assert sum1.matched_verified == 2
    assert mb1.queried == ["Song 0", "Song 1"]

    # Batch 2 (--limit 2): must advance to READY_2, READY_3 without repeating Song 0 / Song 1!
    mb2 = StubMBProvider()
    sum2 = execute_research_batch(cfg, db, limit=2, enable_wikidata_fallback=False, providers_override={"mb": mb2})
    assert sum2.attempted == 2
    assert sum2.matched_verified == 2
    assert mb2.queried == ["Song 2", "Song 3"]

    # Batch 3: all 4 ready tracks are complete -> honest no-work status, 0 provider calls!
    mb3 = StubMBProvider()
    sum3 = execute_research_batch(cfg, db, limit=2, enable_wikidata_fallback=False, providers_override={"mb": mb3})
    assert sum3.attempted == 0
    assert "already been researched" in sum3.no_work_reason or "missing identity" in sum3.no_work_reason
    assert mb3.queried == []

    # Transient outage during --force-refresh MUST preserve old valid candidates on READY_0!
    mb_down = StubMBProvider(fail_transient=True)
    sum_fail = execute_research_batch(cfg, db, limit=1, force_refresh=True, enable_wikidata_fallback=False, providers_override={"mb": mb_down})
    assert sum_fail.failed_retryable == 1
    retained = db.get_candidates("READY_0")
    assert len(retained) == 1
    assert retained[0].title == "Song 0"
