"""
Mandatory regression test suite for MusicVault v2.0.2 Research Reliability Hotfix.
Uses mocked HTTP, deterministic fake clocks/sleep, and temporary SQLite databases (zero real API
traffic, zero Drive/Sheet writes, and zero touches to user HOME/DB).

Covers:
1. Retry-After (integer seconds + RFC HTTP-date) and provider-specific RateLimit-Reset / X-RateLimit-Reset
   (delta vs Unix epoch), honoring multi-hour server waits (>3600s) without shortening.
2. Sleep budget exhaustion deferral without sleeping or sending extra requests, while continuing other providers.
3. HTTP 429, MusicBrainz 503 rate-limit vs generic 5xx distinction, monotonic cooldown expiry,
   proactive 0-remaining window cooldown, enforced MusicBrainz >= 1.1s pacing, and cross-restart
   cooldown persistence in minimal operational state (zero cached restricted payloads).
4. Hard auth (401/403) and documented quota (402/429) blocks vs naive 'explanation' substring guard,
   per-run request caps (BUDGET_EXHAUSTED), and cache hits bypassing request cap.
5. Mixed provider outcomes (empty + 429, partial + cooldown), alternate orientation transient failure
   auditing, all-providers-deferred batch stop without consuming track attempts, and honest deferred
   retry messages when queue waits on future retry_until_utc.
6. Verified recording identity without genre vs routing readiness, and review-only protection ensuring
   Wikidata inferred hints and title-only matches NEVER authorize routing in live preview.
7. Non-destructive transactional candidate merging, deduplication, no false multi-source promotion from
   duplicate single-provider runs, remix/version isolation, preservation of strong evidence against
   empty/weaker responses, and atomic rollback on error.
8. Selective dry-run-first research recovery (preview, verified backup requirement, apply, cooldown
   respect, exclusion of quarantined/permanent_unsupported/stale, separate reporting of ambiguous
   records, and strict idempotency) plus redaction of tokens, private contact emails, and full Drive IDs.
"""
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from musicvault.__main__ import main as cli_main
from musicvault.cli.commands import run_preview, run_recover_research, run_research
from musicvault.config import Config
from musicvault.core.organizer import evaluate_live_preview
from musicvault.core.researcher import execute_research_batch
from musicvault.core.rules import field_decision
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.providers.audiometa import AudioMetaProvider
from musicvault.providers.base import (
    ProviderCallOutcome,
    ProviderStatus,
    parse_provider_reset_wait,
    parse_retry_after,
)
from musicvault.providers.discogs import DiscogsProvider
from musicvault.providers.musicbrainz import MusicBrainzProvider
from musicvault.providers.sonovault import SonoVaultProvider
from musicvault.services.drive import DriveService
from musicvault.services.sheets import SheetsService, validate_and_index_tracks_sheet


class FakeClock:
    """Deterministic monotonic + UTC clock and sleep recorder for zero-wait provider tests."""

    def __init__(self, start_utc: Optional[datetime] = None):
        self.utc = start_utc or datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc)
        self.mono = 1000.0
        self.sleeps: List[float] = []

    def now_utc(self) -> datetime:
        return self.utc

    def now_mono(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        s = max(0.0, float(seconds))
        self.sleeps.append(round(s, 4))
        self.mono += s
        self.utc += timedelta(seconds=s)

    def advance(self, seconds: float) -> None:
        s = float(seconds)
        self.mono += s
        self.utc += timedelta(seconds=s)


class FakeResp:
    def __init__(self, status_code: int, json_data: Any = None, text: str = "", headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (
            json.dumps(json_data) if (json_data is not None and not isinstance(json_data, Exception)) else ""
        )
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def _seed_live_track(
    db: Database,
    file_id: str,
    name: str,
    artist: str = "Act of Rage",
    title: str = "Living Fast",
    version: str = "v1",
    duration: float = 240.0,
    isrc: str = "",
    identity_confidence: str = "verified_embedded",
) -> None:
    db.save_track(
        Track(
            file_id=file_id,
            name=name,
            size=1048576,
            md5=version,
            parent_id="SRC_1",
            observed_parent_id="SRC_1",
            configured_source_id="SRC_1",
            record_origin="live_scan",
            is_live_source=True,
            quarantined=False,
            hydration_status="success" if identity_confidence == "verified_embedded" else "partial",
            hydration_version=version,
            hydration_method="embedded_ranged",
            metadata=TrackMetadata(
                version=version,
                artist=artist,
                title=title,
                duration=duration,
                isrc=isrc,
                identity_origin="embedded" if identity_confidence == "verified_embedded" else "filename_hint",
                identity_confidence=identity_confidence,
            ),
        )
    )


def test_retry_after_http_date_epoch_vs_delta_and_unshortened_long_waits():
    ref = datetime(2026, 11, 26, 4, 0, 0, tzinfo=timezone.utc)

    # 1. Integer seconds including multi-hour (>3600s) server waits — NEVER shortened!
    assert parse_retry_after("7200", now_utc=ref) == 7200.0
    assert parse_retry_after("Wed, 26 Nov 2026 07:00:00 GMT", now_utc=ref) == 10800.0

    # 2. MusicBrainz X-RateLimit-Reset uses Unix epoch seconds
    mb_epoch = int((ref + timedelta(seconds=95)).timestamp())
    assert parse_provider_reset_wait({"X-RateLimit-Reset": str(mb_epoch)}, "musicbrainz", now_utc=ref) == pytest.approx(95.0)

    # 3. IETF RateLimit-Reset uses delta-seconds on SonoVault/standard gateways, with epoch safety guard
    assert parse_provider_reset_wait({"RateLimit-Reset": "42"}, "sonovault", now_utc=ref) == 42.0
    assert parse_provider_reset_wait({"RateLimit-Reset": str(mb_epoch)}, "sonovault", now_utc=ref) == pytest.approx(95.0)

    # 4. Discogs X-Discogs-Ratelimit-Remaining == 0 defaults to 60s window when no Retry-After present
    assert parse_provider_reset_wait({"X-Discogs-Ratelimit-Remaining": "0"}, "discogs", now_utc=ref) == 60.0


def test_sleep_budget_deferral_cooldown_expiry_restart_persistence_and_mb_pacing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    db = Database(tmp_path / "cd_persist.db")

    # 1. MusicBrainz constructor NEVER allows zero-delay bypass even if default_delay=0.0 is passed
    mb = MusicBrainzProvider(
        contact="https://github.com/example/repo",
        db=db,
        default_delay=0.0,
        max_retry_sleep_seconds=15.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert mb.default_delay >= 1.1

    # Verify two consecutive MusicBrainz calls enforce >= 1.1s pacing sleep
    mb_calls = 0

    def _mb_pacing_mock(url: str, **kwargs):
        nonlocal mb_calls
        mb_calls += 1
        return FakeResp(200, {"recordings": []})

    monkeypatch.setattr(mb.session, "get", _mb_pacing_mock)
    mb.request_detailed("https://musicbrainz.org/ws/2/recording", params={"q": "1"}, use_cache=False)
    mb.request_detailed("https://musicbrainz.org/ws/2/recording", params={"q": "2"}, use_cache=False)
    assert mb_calls == 2
    assert any(s >= 1.1 for s in clock.sleeps)

    # 2. SonoVault 429 with Retry-After=45s (> max_retry_sleep_seconds=15s):
    #    Must NOT sleep 45s or retry; must immediately defer SonoVault and persist cooldown!
    clock.sleeps.clear()
    sono = SonoVaultProvider(
        api_key="sv_secret_test_key",
        db=db,
        provider_terms_cleared=False,
        default_delay=0.0,
        max_retry_sleep_seconds=15.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    sv_http_hits = 0

    def _sv_mock(*args, **kwargs):
        nonlocal sv_http_hits
        sv_http_hits += 1
        if sv_http_hits == 1:
            # Includes 'explanation' to verify naive 'plan' substring does NOT falsely trigger QUOTA_EXCEEDED!
            return FakeResp(
                429,
                {"explanation": "Rate limit exceeded; slow down"},
                headers={"Retry-After": "45"},
            )
        return FakeResp(
            200,
            {"results": [{"title": "Living Fast", "artists": [{"name": "Act of Rage"}], "bpm": 158, "duration": 240, "genre": "Hardstyle"}]},
        )

    monkeypatch.setattr(sono.session, "get", _sv_mock)
    cands1, out1 = sono.search_detailed("Act of Rage", "Living Fast")
    assert cands1 == []
    assert out1.status == ProviderStatus.RATE_LIMITED
    assert out1.error_kind == "http_429_rate_limit"
    assert out1.retry_after_seconds == 45.0
    assert sv_http_hits == 1  # Did NOT retry when wait exceeded sleep budget!
    assert clock.sleeps == []  # Did NOT sleep at all!

    # Immediate follow-up call in-process returns COOLDOWN_DEFERRED (NOT BUDGET_EXHAUSTED!) with 0 HTTP calls
    cands2, out2 = sono.search_detailed("Act of Rage", "Second Song")
    assert out2.status == ProviderStatus.COOLDOWN_DEFERRED
    assert out2.deferred_by_cooldown is True
    assert out2.request_made is False
    assert sv_http_hits == 1

    # 3. Simulate process restart after 10s (35s still remaining): new SonoVaultProvider loads operational cooldown
    #    from SQLite without storing restricted response payloads!
    clock.advance(10.0)
    with db._get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_cache WHERE provider='sonovault'").fetchone()[0] == 0
        op_row = conn.execute("SELECT * FROM provider_operational_state WHERE provider='sonovault'").fetchone()
        assert op_row is not None
        assert "sv_secret_test_key" not in json.dumps(dict(op_row))

    sono_restarted = SonoVaultProvider(
        api_key="sv_secret_test_key",
        db=db,
        provider_terms_cleared=False,
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(sono_restarted.session, "get", _sv_mock)
    _, out_still_cd = sono_restarted.search_detailed("Act of Rage", "Living Fast")
    assert out_still_cd.status == ProviderStatus.COOLDOWN_DEFERRED
    assert out_still_cd.retry_after_seconds == pytest.approx(35.0, abs=0.5)
    assert sv_http_hits == 1

    # 4. Advance fake clock past remaining 36s -> cooldown expires and provider succeeds automatically!
    clock.advance(36.0)
    cands_ok, out_ok = sono_restarted.search_detailed("Act of Rage", "Living Fast")
    assert out_ok.status == ProviderStatus.OK
    assert len(cands_ok) == 1
    assert sv_http_hits == 2


def test_mb_503_vs_generic_5xx_hard_blocks_proactive_window_and_request_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock()
    db = Database(tmp_path / "caps_blocks.db")

    # 1. MusicBrainz 503 rate-limit vs generic 503 server maintenance
    mb = MusicBrainzProvider(
        db=db,
        max_retry_sleep_seconds=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(
        mb.session,
        "get",
        lambda *a, **kw: FakeResp(503, {"error": "Your requests are exceeding the allowable rate limit."}, headers={"Retry-After": "20"}),
    )
    out_mb_rl = mb.request_detailed("https://musicbrainz.org/ws/2/recording", params={"q": "rl"}, use_cache=False)
    assert out_mb_rl.status == ProviderStatus.RATE_LIMITED
    assert out_mb_rl.error_kind == "musicbrainz_503_rate_limit"
    assert mb.stats["mb_503_rate_limited"] == 1

    clock.advance(25.0)
    monkeypatch.setattr(
        mb.session,
        "get",
        lambda *a, **kw: FakeResp(503, "<html>Service Unavailable - maintenance</html>"),
    )
    out_mb_503_srv = mb.request_detailed("https://musicbrainz.org/ws/2/recording", params={"q": "srv"}, use_cache=False)
    assert out_mb_503_srv.status == ProviderStatus.SERVER_ERROR
    assert out_mb_503_srv.error_kind == "http_5xx_server_error"

    # 2. Hard auth block (401) stays AUTH_ERROR on subsequent calls (not BUDGET_EXHAUSTED)
    am = AudioMetaProvider(
        api_key="amsk_invalid_xyz",
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    am_hits = 0

    def _am_401(*a, **kw):
        nonlocal am_hits
        am_hits += 1
        return FakeResp(401, {"error": "Invalid token"})

    monkeypatch.setattr(am.session, "get", _am_401)
    _, first_401 = am.search_detailed("Artist", "Song")
    _, second_401 = am.search_detailed("Artist", "Song 2")
    assert first_401.status == ProviderStatus.AUTH_ERROR
    assert second_401.status == ProviderStatus.AUTH_ERROR
    assert second_401.request_made is False
    assert am_hits == 1
    assert am.stats["skipped_blocked"] == 1

    # 3. Discogs proactive 0-remaining window sets 60s cooldown WITHOUT stalling current 200 OK,
    #    and cache hits STILL work even when max_requests cap or cooldown is reached!
    dc = DiscogsProvider(
        token="dc_tok_valid",
        max_requests=1,
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(
        dc.session,
        "get",
        lambda *a, **kw: FakeResp(200, {"results": []}, headers={"X-Discogs-Ratelimit-Remaining": "0"}),
    )
    clock.sleeps.clear()
    out_dc_ok = dc.request_detailed("https://api.discogs.com/database/search", params={"q": "cached_q"})
    assert out_dc_ok.status == ProviderStatus.OK
    assert clock.sleeps == []  # Returned 200 OK immediately without sleeping!

    # Same query hits session cache cleanly even while in cooldown and at max_requests=1!
    out_dc_cached = dc.request_detailed("https://api.discogs.com/database/search", params={"q": "cached_q"})
    assert out_dc_cached.status == ProviderStatus.OK
    assert out_dc_cached.from_cache is True
    assert dc.stats["cache_hits"] == 1

    # Uncached query sees active 60s cooldown first, then after 61s sees BUDGET_EXHAUSTED (max_requests=1)
    out_dc_cd = dc.request_detailed("https://api.discogs.com/database/search", params={"q": "uncached_2"})
    assert out_dc_cd.status == ProviderStatus.COOLDOWN_DEFERRED
    clock.advance(61.0)
    out_dc_cap = dc.request_detailed("https://api.discogs.com/database/search", params={"q": "uncached_2"})
    assert out_dc_cap.status == ProviderStatus.BUDGET_EXHAUSTED
    assert out_dc_cap.request_made is False


def test_mixed_outcomes_long_wait_alternate_orientation_failure_and_deferred_messages(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock()
    cfg = Config()
    cfg.set("SONOVAULT_API_KEY", "sv_live_key_1")
    db = Database(cfg.db_path)

    # Track 1 ('00_MIX_1' runs first): verified identity; MB returns EMPTY while SonoVault returns 429 with Retry-After=7200s (>3600s!)
    _seed_live_track(db, "00_MIX_1", "Act of Rage - Living Fast.flac", artist="Act of Rage", title="Living Fast", version="v_mix1")
    # Track 2 ('AMB_1' runs second): ambiguous filename ('Title - Artist'); primary returns EMPTY, swapped hits TIMEOUT!
    _seed_live_track(
        db,
        "AMB_1",
        "Believe - Jesse Bloch.wav",
        artist="Believe",
        title="Jesse Bloch",
        version="v_amb1",
        identity_confidence="filename_ambiguous",
    )

    sono = SonoVaultProvider(
        api_key="sv_live_key_1",
        db=db,
        default_delay=0.0,
        max_retry_sleep_seconds=5.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    mb = MusicBrainzProvider(
        db=db,
        max_retry_sleep_seconds=5.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )

    sv_mode = {"fail_429": True}

    def _sv_get(*a, **kw):
        if sv_mode["fail_429"]:
            return FakeResp(429, {"error": "Too many requests"}, headers={"Retry-After": "7200"})
        return FakeResp(
            200,
            {"results": [{"title": "Living Fast", "artists": [{"name": "Act of Rage"}], "bpm": 158, "key_alphanumeric": "11A", "duration": 240, "genre": "Hardstyle"}]},
        )

    mb_queries: List[str] = []

    def _mb_get(url: str, params: Optional[Dict[str, Any]] = None, **kw):
        q = str((params or {}).get("query") or "")
        mb_queries.append(q)
        # Fail transiently ONLY on the swapped orientation ('artist:"Jesse Bloch"')!
        if 'artist:"Jesse Bloch"' in q:
            import requests as req_mod
            raise req_mod.Timeout("Swapped orientation timed out")
        return FakeResp(200, {"recordings": []})

    monkeypatch.setattr(sono.session, "get", _sv_get)
    monkeypatch.setattr(mb.session, "get", _mb_get)

    sum1 = execute_research_batch(
        cfg,
        db,
        limit=10,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono, "mb": mb},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum1.attempted == 2
    assert sum1.no_match == 0  # Neither track was falsely marked definitive no_match!
    assert sum1.incomplete_tracks == 2

    t_mix = db.get_track("00_MIX_1")
    assert t_mix is not None
    assert t_mix.research_status == "retryable_failure"
    assert t_mix.research_incomplete is True
    # Must honor full 7200s wait beyond the old 3600s cap!
    retry_dt = datetime.fromisoformat(t_mix.research_next_retry_utc)
    assert (retry_dt - datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc)).total_seconds() >= 7200.0

    t_amb = db.get_track("AMB_1")
    assert t_amb is not None
    assert t_amb.research_status == "retryable_failure"
    amb_audit = json.loads(t_amb.provider_calls_json)
    # Swapped alternate orientation failure is explicitly audited, never discarded!
    assert amb_audit["MusicBrainz"]["primary_query"]["status"] == "empty"
    assert amb_audit["MusicBrainz"]["alternate_query"]["orientation"] == "swapped"
    assert amb_audit["MusicBrainz"]["alternate_query"]["status"] == "timeout"

    # Re-running 5s later (while both 00_MIX_1 [7200s] and AMB_1 [15s] retries are deferred)
    # must report honest deferred-retry reason, NOT 'already researched'!
    clock.advance(5.0)
    sum_wait = execute_research_batch(
        cfg,
        db,
        limit=10,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono, "mb": mb},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_wait.attempted == 0
    assert "deferred until" in sum_wait.no_work_reason
    assert "already been researched" not in sum_wait.no_work_reason

    # Advance past 7200s cooldown; SonoVault recovers! 00_MIX_1 reuses MB's completed same-version empty lookup
    # without re-querying MusicBrainz for 00_MIX_1!
    clock.advance(7260.0)
    sv_mode["fail_429"] = False
    mb_queries.clear()
    sono2 = SonoVaultProvider(
        api_key="sv_live_key_1",
        db=db,
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    mb2 = MusicBrainzProvider(
        db=db,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    def _mb2_get(url: str, params: Optional[Dict[str, Any]] = None, **kw):
        mb_queries.append(str((params or {}).get("query") or ""))
        return FakeResp(200, {"recordings": []})

    monkeypatch.setattr(sono2.session, "get", _sv_get)
    monkeypatch.setattr(mb2.session, "get", _mb2_get)

    sum2 = execute_research_batch(
        cfg,
        db,
        limit=10,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono2, "mb": mb2},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum2.attempted == 2
    t_mix_done = db.get_track("00_MIX_1")
    assert t_mix_done is not None and t_mix_done.research_status == "success" and t_mix_done.research_incomplete is False
    # MusicBrainz WAS queried to finish AMB_1's swapped orientation, and NOT re-queried for 00_MIX_1 ('Living Fast')!
    assert any("Jesse Bloch" in q for q in mb_queries)
    assert not any("Living Fast" in q for q in mb_queries)


def test_all_providers_deferred_consumes_zero_attempts_and_verified_no_genre_vs_preview_safety(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock()
    cfg = Config()
    db = Database(cfg.db_path)

    _seed_live_track(db, "NO_GENRE_1", "Artist X - Genreless ID.flac", artist="Artist X", title="Genreless ID", version="vg1")

    # 1. Put MusicBrainz into persisted 300s cooldown; batch must pause cleanly with 0 attempts consumed!
    db.set_provider_operational_state(
        "musicbrainz",
        cooldown_until_utc=(clock.now_utc() + timedelta(seconds=300)).isoformat(),
        last_status="rate_limited",
        last_http_status=503,
        error_kind="musicbrainz_503_rate_limit",
        reason="HTTP 503 rate limited",
        now_utc=clock.now_utc(),
    )
    mb_cd = MusicBrainzProvider(
        db=db,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    sum_cd = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=False,
        providers_override={"mb": mb_cd},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_cd.attempted == 0
    assert sum_cd.paused_provider_cooldown is True
    assert sum_cd.deferred_without_attempt == 1
    tr_wait = db.get_track("NO_GENRE_1")
    assert tr_wait is not None and tr_wait.research_attempts == 0

    # 2. Advance past 300s cooldown; AudioMeta verifies duration (BPM+Key ONLY, no genre) + Wikidata gives inferred hint!
    clock.advance(305.0)
    am_ok = AudioMetaProvider(
        api_key="amsk_valid",
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(
        am_ok.session,
        "get",
        lambda *a, **kw: FakeResp(
            200,
            {"tracks": [{"id": "am1", "name": "Genreless ID", "artists": [{"name": "Artist X"}], "duration_ms": 240000, "audio_features": {"tempo": 155.0, "key": 9, "mode": 0}}]},
        ),
    )

    class StubWikiHint:
        stats = {"calls": 1, "cache_hits": 0}

        def research_track_detailed(self, artist: str, title: str):
            return (
                {"genre_hints": [{"url": "https://www.wikidata.org/wiki/Q42", "parent_genres": ["Hardstyle"]}]},
                ProviderCallOutcome(provider="OnlineResearch", status=ProviderStatus.OK, http_status=200, request_made=True),
            )

    sum_ng = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=True,
        providers_override={"am": am_ok, "wiki": StubWikiHint()},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_ng.matched_verified == 1
    assert sum_ng.matched_verified_no_genre == 1
    assert sum_ng.matched_verified_with_genre == 0

    tr_ng = db.get_track("NO_GENRE_1")
    assert tr_ng is not None and tr_ng.research_status == "success"
    meta_audit = json.loads(tr_ng.provider_calls_json)["_meta"]
    assert meta_audit["verified_identity"] is True
    assert meta_audit["has_verified_genre"] is False
    assert meta_audit["routing_ready_from_catalogue"] is False

    # 3. Verify live preview NEVER promotes inferred Wikidata or Title-only candidates to eligible/authorized routing!
    headers = ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [""] * 8 + ["Drive ID", "Notes"]
    sheet_idx = validate_and_index_tracks_sheet(
        headers,
        [["Genreless ID", "Artist X", "", "", "", "", "", "", "", "", "", "", "", "", "NO_GENRE_1", ""]],
    )
    cands_stored = db.get_candidates("NO_GENRE_1")
    cands_with_title_only = cands_stored + [
        Candidate(
            provider="MusicBrainz",
            title="Genreless ID",
            artists=["Artist X"],
            genre=["Hardstyle"],
            verification="Title-only review",
            match_confidence="title_only_unverified",
            source_family="musicbrainz",
        )
    ]
    prev = evaluate_live_preview(
        live_tracks=[tr_ng],
        candidates_by_id={"NO_GENRE_1": cands_with_title_only},
        sheet_index=sheet_idx,
        source_folder_id="SRC_1",
        vault_folder_id="VAULT_1",
        vault_folders={"Hardstyle": ["F_HS"]},
    )
    assert prev.eligible_count == 0
    assert prev.authorized_count == 0
    assert prev.executable_count == 0
    assert prev.rows[0].eligible is False


def test_non_destructive_candidate_merge_dedup_version_isolation_and_atomic_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "merge_atomic.db")
    _seed_live_track(db, "MRG_1", "Act of Rage - Living Fast.flac", version="ver_A", isrc="NLFL72401034")

    t0 = datetime(2026, 11, 26, 1, 0, 0, tzinfo=timezone.utc)
    # 1. Store strong SonoVault ISRC-supported candidate
    db.update_research_result(
        "MRG_1",
        status="success",
        version="ver_A",
        candidates=[
            Candidate(
                provider="SonoVault",
                title="Living Fast",
                artists=["Act of Rage"],
                duration=240.0,
                isrc="NLFL72401034",
                bpm=158.0,
                key="11A",
                genre=["Hardstyle"],
                verification="ISRC supported",
                content_version="ver_A",
                source_family="sonovault",
            )
        ],
        provider_calls={"SonoVault": {"status": "ok"}},
        now_utc=t0,
    )
    c_initial = db.get_candidates("MRG_1")
    assert len(c_initial) == 1
    orig_created_utc = c_initial[0].created_utc

    # 2. Second pass: SonoVault returns weaker 'Text only' (missing BPM), Wikidata returns inferred hint,
    #     incompatible remix ('Version conflict') + stale version ('ver_OLD') arrive, and MusicBrainz adds Duration supported:
    #    - SonoVault MUST keep 'ISRC supported', bpm=158.0, and original created_utc!
    #    - Duplicate SonoVault re-merge MUST NOT create a 2nd SonoVault row!
    #    - Version conflict and mismatched content_version MUST be rejected!
    t1 = t0 + timedelta(minutes=10)
    merged = db.update_research_result(
        "MRG_1",
        status="success",
        version="ver_A",
        candidates=[
            Candidate(
                provider="SonoVault",
                title="Living Fast",
                artists=["Act of Rage"],
                bpm=None,
                genre=["Hardstyle", "Rawstyle"],
                verification="Text only",
                content_version="ver_A",
                source_family="sonovault",
            ),
            Candidate(
                provider="SonoVault",
                title="Living Fast (Darren Styles Remix)",
                artists=["Act of Rage"],
                verification="Version conflict",
                content_version="ver_A",
                source_family="sonovault",
            ),
            Candidate(
                provider="Discogs release",
                title="Living Fast",
                artists=["Act of Rage"],
                genre=["Hardstyle"],
                verification="Duration supported",
                content_version="ver_OLD_STALE",
                source_family="discogs",
            ),
            Candidate(
                provider="Wikidata (inferred)",
                title="Living Fast",
                artists=["Act of Rage"],
                genre=["Techno"],
                url="https://www.wikidata.org/wiki/Q99",
                verification="Review only (inferred)",
                content_version="ver_A",
                source_family="wikidata",
            ),
        ],
        provider_calls={"SonoVault": {"status": "ok"}},
        now_utc=t1,
    )
    sv_rows = [c for c in merged if c.source_family == "sonovault"]
    assert len(sv_rows) == 1  # Deduplicated!
    assert sv_rows[0].verification == "ISRC supported"  # Strong verification preserved!
    assert sv_rows[0].bpm == 158.0  # Existing BPM preserved!
    assert sv_rows[0].genre == ["Hardstyle"]  # Weaker 'Text only' hit did NOT overwrite/pollute verified genre!
    assert sv_rows[0].created_utc == orig_created_utc  # Original timestamp preserved!
    assert not any(c.verification == "Version conflict" for c in merged)
    assert not any(c.content_version == "ver_OLD_STALE" for c in merged)

    # Equal-rank authoritative refresh DOES enrich/update supplementary subgenre while keeping created_utc:
    merged = db.update_research_result(
        "MRG_1",
        status="success",
        version="ver_A",
        candidates=[
            Candidate(
                provider="SonoVault",
                title="Living Fast",
                artists=["Act of Rage"],
                isrc="NLFL72401034",
                genre=["Hardstyle", "Rawstyle"],
                verification="ISRC supported",
                content_version="ver_A",
                source_family="sonovault",
            )
        ],
        provider_calls={"SonoVault": {"status": "ok"}},
        now_utc=t1 + timedelta(minutes=1),
    )
    sv_refreshed = [c for c in merged if c.source_family == "sonovault"]
    assert len(sv_refreshed) == 1
    assert sv_refreshed[0].bpm == 158.0
    assert "Rawstyle" in sv_refreshed[0].genre
    assert sv_refreshed[0].created_utc == orig_created_utc

    # Single catalogue family ('sonovault') + Wikidata MUST stay MEDIUM, NEVER promoted to HIGH or downgraded by Wikidata!
    assert field_decision(merged, "bpm", 158.0)[0] == "MEDIUM"
    assert field_decision(merged, "genre", "Hardstyle")[0] == "MEDIUM"

    # 3. Empty update (0 new candidates) MUST NOT erase stored SonoVault candidate!
    after_empty = db.update_research_result(
        "MRG_1",
        status="success",
        version="ver_A",
        candidates=[],
        provider_calls={"MusicBrainz": {"status": "empty"}},
        now_utc=t1 + timedelta(minutes=5),
    )
    assert any(c.source_family == "sonovault" and c.verification == "ISRC supported" for c in after_empty)

    # 4. Atomic rollback: if json serialization / DB write raises mid-transaction, neither candidates nor track mutates!
    class UnserializableObj:
        pass

    with pytest.raises(TypeError):
        db.update_research_result(
            "MRG_1",
            status="no_match",
            version="ver_A",
            candidates=[
                Candidate(
                    provider="MusicBrainz",
                    title="Living Fast",
                    artists=["Act of Rage"],
                    duration=240.0,
                    genre=["Hardstyle"],
                    verification="Duration supported",
                    content_version="ver_A",
                    source_family="musicbrainz",
                )
            ],
            provider_calls={"broken": UnserializableObj()},
        )
    tr_after_err = db.get_track("MRG_1")
    assert tr_after_err is not None and tr_after_err.research_status == "success"
    # MusicBrainz candidate inserted before provider_calls serialization failure was rolled back atomically!
    assert not any(c.source_family == "musicbrainz" for c in db.get_candidates("MRG_1"))

    # 5. Version isolation: changing version to 'ver_B' marks 'ver_A' candidates stale!
    db.update_research_result(
        "MRG_1",
        status="partial",
        version="ver_B",
        candidates=[
            Candidate(
                provider="MusicBrainz",
                title="Living Fast",
                artists=["Act of Rage"],
                verification="Text only",
                content_version="ver_B",
                source_family="musicbrainz",
            )
        ],
        provider_calls={"MusicBrainz": {"status": "ok"}},
    )
    active_v_b = db.get_candidates("MRG_1", include_stale=False)
    all_incl_stale = db.get_candidates("MRG_1", include_stale=True)
    assert len(active_v_b) == 1 and active_v_b[0].content_version == "ver_B"
    assert any(c.is_stale and c.content_version == "ver_A" for c in all_incl_stale)


def test_selective_recovery_preview_apply_idempotency_exclusions_and_redaction(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    cfg = Config()
    cfg.set("SONOVAULT_API_KEY", "sv_super_secret_token_777")
    cfg.set("MUSICBRAINZ_CONTACT", "private.dj.owner@example.org")
    db = Database(cfg.db_path)

    # Seed active cooldown on sonovault to verify recovery respects active cooldowns
    future_cd = (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
    db.set_provider_operational_state(
        "sonovault",
        cooldown_until_utc=future_cd,
        last_status="rate_limited",
        last_http_status=429,
        reason="HTTP 429 rate limited",
    )

    # 1. Recoverable: no_match where SonoVault had budget_exhausted + MB empty
    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM", "Artist 1 - Stranded NoMatch.flac", version="v1")
    db.update_research_result(
        "1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM",
        status="no_match",
        version="v1",
        candidates=[],
        provider_calls={
            "SonoVault": {"status": "budget_exhausted", "request_made": False},
            "MusicBrainz": {"status": "empty", "request_made": True},
        },
    )

    # 2. Recoverable: partial (with preserved candidate!) where AudioMeta had rate_limited
    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxREC_PT", "Artist 2 - Stranded Partial.flac", version="v2")
    db.update_research_result(
        "1v3jdlhhx3hqfY6ivYBut3DcJxREC_PT",
        status="partial",
        version="v2",
        candidates=[
            Candidate(
                provider="Wikidata (inferred)",
                title="Stranded Partial",
                artists=["Artist 2"],
                genre=["Hardstyle"],
                verification="Review only (inferred)",
                content_version="v2",
                source_family="wikidata",
            )
        ],
        provider_calls={
            "AudioMeta": {"status": "rate_limited", "http": 429, "request_made": True},
            "MusicBrainz": {"status": "empty", "request_made": True},
        },
    )

    # 3. Recoverable: success (verified duration, no genre) where MusicBrainz had server_error
    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxREC_OK", "Artist 3 - Incomplete Success.flac", version="v3")
    db.update_research_result(
        "1v3jdlhhx3hqfY6ivYBut3DcJxREC_OK",
        status="success",
        version="v3",
        candidates=[
            Candidate(
                provider="Audiometa",
                title="Incomplete Success",
                artists=["Artist 3"],
                duration=240.0,
                bpm=150.0,
                verification="Duration supported",
                content_version="v3",
                source_family="audiometa",
            )
        ],
        provider_calls={
            "AudioMeta": {"status": "ok", "http": 200, "request_made": True},
            "MusicBrainz": {"status": "server_error", "http": 503, "request_made": True},
        },
    )

    # 4. Recoverable: exhausted retryable_failure (attempts == 3)
    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxREC_EX", "Artist 4 - Exhausted Retries.flac", version="v4")
    for _ in range(3):
        db.update_research_result(
            "1v3jdlhhx3hqfY6ivYBut3DcJxREC_EX",
            status="retryable_failure",
            version="v4",
            candidates=[],
            provider_calls={"MusicBrainz": {"status": "timeout", "request_made": True}},
        )

    # 5. Ambiguous (MUST NOT BE RESET): legacy empty '{}' audit & v2.0.1 filename_ambiguous no_match & auth_error
    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxAMB_LG", "Artist 5 - Legacy Audit.flac", version="v5")
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE tracks SET research_status = 'no_match', provider_calls_json = '{}' WHERE file_id = '1v3jdlhhx3hqfY6ivYBut3DcJxAMB_LG'"
        )

    _seed_live_track(
        db,
        "1v3jdlhhx3hqfY6ivYBut3DcJxAMB_FN",
        "Song 6 - Artist 6.wav",
        version="v6",
        identity_confidence="filename_ambiguous",
    )
    with db._get_connection() as conn:
        conn.execute(
            """
            UPDATE tracks
            SET research_status = 'no_match',
                provider_calls_json = '{"MusicBrainz": {"status": "empty", "request_made": true}, "SonoVault": {"status": "empty", "request_made": true}}'
            WHERE file_id = '1v3jdlhhx3hqfY6ivYBut3DcJxAMB_FN'
            """
        )

    _seed_live_track(db, "1v3jdlhhx3hqfY6ivYBut3DcJxAMB_AU", "Artist 7 - Auth Blocked.flac", version="v7")
    with db._get_connection() as conn:
        conn.execute(
            """
            UPDATE tracks
            SET research_status = 'unresolved',
                provider_calls_json = '{"SonoVault": {"status": "auth_error", "http": 401}}'
            WHERE file_id = '1v3jdlhhx3hqfY6ivYBut3DcJxAMB_AU'
            """
        )

    # 6. Excluded (MUST NEVER BE ALTERED): quarantined checkpoint row + permanent_unsupported row
    db.import_json_checkpoint_test_data = None
    with db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tracks (
                file_id, name, artist, title, version, is_live_source, quarantined,
                research_status, provider_calls_json
            ) VALUES ('QUAR_1', 'Quarantined.flac', 'A', 'B', 'vq', 0, 1, 'no_match', '{"SonoVault": {"status": "rate_limited"}}')
            """
        )
        conn.execute(
            """
            INSERT INTO tracks (
                file_id, name, artist, title, version, is_live_source, quarantined,
                hydration_status, research_status, provider_calls_json
            ) VALUES ('PERM_1', 'Corrupt.flac', 'A', 'C', 'vp', 1, 0, 'permanent_unsupported', 'permanent_unsupported', '{"SonoVault": {"status": "rate_limited"}}')
            """
        )

    # Step A: Dry-run preview (default) makes ZERO mutations and masks full Drive IDs + private contact/secrets!
    json_preview = isolated_musicvault_environment / "rec_preview.json"
    csv_preview = isolated_musicvault_environment / "rec_preview.csv"
    rc_dry = cli_main(["recover-research", "--export-json", str(json_preview), "--export-csv", str(csv_preview)])
    assert rc_dry == 0
    out_dry = capsys.readouterr().out
    assert "DRY-RUN PREVIEW" in out_dry
    assert "1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM" not in out_dry  # Masked by default!
    assert "sv_super_secret_token_777" not in out_dry
    assert "private.dj.owner@example.org" not in out_dry

    rep_json = json.loads(json_preview.read_text(encoding="utf-8"))
    assert rep_json["counts"]["recoverable_total"] == 4
    assert rep_json["counts"]["ambiguous_total"] == 3
    assert rep_json["counts"]["applied_count"] == 0
    assert "1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM" not in json_preview.read_text(encoding="utf-8")
    with csv_preview.open("r", encoding="utf-8") as fh:
        csv_text = fh.read()
    assert "1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM" not in csv_text

    # Step B: --apply WITHOUT verified backup MUST fail closed and make ZERO mutations!
    rc_no_bak = cli_main(["recover-research", "--apply"])
    assert rc_no_bak == 1
    capsys.readouterr()
    assert db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM").research_status == "no_match"

    # Step C: --apply --create-backup creates verified backup, recovers exact 4 tracks, preserves candidates & hydration,
    #         respects active SonoVault cooldown, and leaves ambiguous/quarantined/permanent_unsupported untouched!
    rc_apply = cli_main(["recover-research", "--apply", "--create-backup"])
    assert rc_apply == 0
    capsys.readouterr()

    t_nm = db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxREC_NM")
    assert t_nm.research_status == "retryable_failure"
    assert t_nm.research_incomplete is True
    assert t_nm.research_attempts == 0
    # SonoVault was the only failed provider on REC_NM and has an active 900s cooldown -> respected!
    assert t_nm.research_next_retry_utc == future_cd

    t_pt = db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxREC_PT")
    assert t_pt.research_status == "partial"
    assert t_pt.research_incomplete is True
    assert len(db.get_candidates("1v3jdlhhx3hqfY6ivYBut3DcJxREC_PT")) == 1  # Candidate preserved!

    t_ok = db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxREC_OK")
    assert t_ok.research_status == "success"
    assert t_ok.research_incomplete is True
    assert len(db.get_candidates("1v3jdlhhx3hqfY6ivYBut3DcJxREC_OK")) == 1  # Candidate preserved!
    assert t_ok.hydration_status == "success"

    # Ambiguous, quarantined, and permanent_unsupported records strictly untouched!
    assert db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxAMB_LG").research_status == "no_match"
    assert db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxAMB_FN").research_status == "no_match"
    assert db.get_track("1v3jdlhhx3hqfY6ivYBut3DcJxAMB_AU").research_status == "unresolved"
    assert db.get_track("QUAR_1").quarantined is True and db.get_track("QUAR_1").research_status == "no_match"
    assert db.get_track("PERM_1").research_status == "permanent_unsupported"

    # Step D: Running --apply a SECOND time is strictly IDEMPOTENT (0 recoverable, 0 applied)!
    rep_idem = run_recover_research(cfg, db, apply=True, create_backup=True)
    assert rep_idem["counts"]["recoverable_total"] == 0
    assert rep_idem["counts"]["applied_count"] == 0

    # Step E: Check private contact & token redaction in research CLI + JSON export (mocking Session.get offline!)
    monkeypatch.setattr(
        "requests.Session.get",
        lambda *a, **kw: FakeResp(
            401,
            {"error": "Auth rejected for private.dj.owner@example.org with sv_super_secret_token_777"},
        ),
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    res_json_path = isolated_musicvault_environment / "res_diag.json"
    cli_main(["research", "--limit", "2", "--export-json", str(res_json_path)])
    res_console = capsys.readouterr().out
    assert "private.dj.owner@example.org" not in res_console
    assert "sv_super_secret_token_777" not in res_console
    assert "private.dj.owner@example.org" not in res_json_path.read_text(encoding="utf-8")


def test_v202_adversarial_review_edge_cases(
    isolated_musicvault_environment: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Regression tests for all independent code-review findings:
    1a. Hard-blocked (401) + cached clean provider with candidates does NOT busy-loop on subsequent runs,
        and timed QUOTA_EXCEEDED preserves full Retry-After in research_next_retry_utc.
    1b. Track needing only a cooling-down provider continues batch for healthy providers on subsequent tracks.
    2a/2b. Partial candidates fetched before a sub-request 429 are retained, and verified recording keeps 'success'.
    2c. Unverified/conflicting prior candidate fields never pollute a newly recording-verified upgrade.
    2d. Re-query executes when (artist, title) is refined at the same file content version.
    3a/3b. Gateway 522 and Retry-After:0 + RateLimit-Reset:60 honor the positive server wait.
    """
    clock = FakeClock()
    cfg = Config()
    cfg.set("SONOVAULT_API_KEY", "sv_key_rev")
    db = Database(cfg.db_path)

    # 3b & 3a: Retry-After: 0 alongside RateLimit-Reset: 60 returns 60.0 (never masked by 0), and HTTP 522 honors Retry-After!
    assert parse_provider_reset_wait({"Retry-After": "0", "RateLimit-Reset": "60"}, "sonovault", now_utc=clock.now_utc()) == 60.0
    sv_gw = SonoVaultProvider(
        api_key="sv_gw_key",
        default_delay=0.0,
        max_retry_sleep_seconds=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(sv_gw.session, "get", lambda *a, **kw: FakeResp(522, "Cloudflare origin timeout", headers={"Retry-After": "90"}))
    out_522 = sv_gw.request_detailed("https://api.sonovault.now/v1/tracks/search", params={"q": "x"}, use_cache=False)
    assert out_522.status == ProviderStatus.SERVER_ERROR
    assert out_522.retry_after_seconds == 90.0

    # 2c: Conflicting prior candidate ('Duration conflict' with bogus genre 'Techno' & bpm 128.0) MUST NOT
    #     pollute an authoritative 'ISRC supported' upgrade ('Hardstyle')!
    _seed_live_track(db, "UPG_1", "Act of Rage - Living Fast.flac", version="v_upg", isrc="NLFL72401034")
    db.update_research_result(
        "UPG_1",
        status="partial",
        version="v_upg",
        candidates=[
            Candidate(
                provider="SonoVault",
                title="Living Fast",
                artists=["Act of Rage"],
                bpm=128.0,
                key="1A",
                genre=["Techno"],
                verification="Duration conflict",
                content_version="v_upg",
                source_family="sonovault",
            )
        ],
        provider_calls={"SonoVault": {"status": "ok"}},
        now_utc=clock.now_utc(),
    )
    upgraded = db.update_research_result(
        "UPG_1",
        status="success",
        version="v_upg",
        candidates=[
            Candidate(
                provider="SonoVault",
                title="Living Fast",
                artists=["Act of Rage"],
                duration=240.0,
                isrc="NLFL72401034",
                bpm=None,
                key=None,
                genre=["Hardstyle"],
                verification="ISRC supported",
                content_version="v_upg",
                source_family="sonovault",
            )
        ],
        provider_calls={"SonoVault": {"status": "ok"}},
        now_utc=clock.now_utc(),
    )
    assert len(upgraded) == 1
    assert upgraded[0].verification == "ISRC supported"
    assert upgraded[0].genre == ["Hardstyle"]  # Bogus 'Techno' from Duration conflict NOT unioned!
    assert upgraded[0].bpm is None  # Bogus 128.0 from Duration conflict NOT inherited!
    # Clean UPG_1 out of queue
    with db._get_connection() as conn:
        conn.execute("UPDATE tracks SET is_live_source = 0 WHERE file_id = 'UPG_1'")

    # 1a + 2a + 2b: Discogs returns Hit #1 ('Duration supported') then Hit #2's release detail hits 429 (90s),
    #               while SonoVault hits 401 AUTH_ERROR:
    #               - Hit #1 candidate MUST be retained (2a)!
    #               - Track status MUST stay 'success' with research_incomplete=True (2b)!
    _seed_live_track(db, "PART_SUB_1", "Act of Rage - Living Fast.flac", version="v_ps1")
    dc_sub = DiscogsProvider(
        token="dc_tok_sub",
        db=db,
        default_delay=0.0,
        max_retry_sleep_seconds=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )

    def _dc_sub_mock(url: str, **kw):
        if "database/search" in url:
            return FakeResp(200, {"results": [{"id": 101}, {"id": 102}]})
        if url.endswith("/101"):
            return FakeResp(
                200,
                {"uri": "https://discogs.com/release/101", "genres": ["Electronic"], "styles": ["Hardstyle"], "tracklist": [{"title": "Living Fast", "artists": [{"name": "Act of Rage"}], "duration": "4:00"}]},
            )
        return FakeResp(429, {"message": "Slow down"}, headers={"Retry-After": "90"})

    monkeypatch.setattr(dc_sub.session, "get", _dc_sub_mock)
    sum_sub = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=False,
        providers_override={"dc": dc_sub},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_sub.matched_verified == 1
    c_sub = db.get_candidates("PART_SUB_1")
    assert len(c_sub) == 1 and c_sub[0].verification == "Duration supported"
    t_sub = db.get_track("PART_SUB_1")
    assert t_sub.research_status == "success" and t_sub.research_incomplete is True

    # 1a (no busy-loop on untimed hard block + cached clean provider):
    _seed_live_track(db, "HB_1", "Act of Rage - Song HB.flac", artist="Act of Rage", title="Song HB", version="v_hb1")
    # Put PART_SUB_1 future retry past current clock
    clock.advance(1.0)
    sono_401 = SonoVaultProvider(
        api_key="sv_bad_401",
        db=db,
        default_delay=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    mb_cached = MusicBrainzProvider(
        db=db,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(sono_401.session, "get", lambda *a, **kw: FakeResp(401, {"error": "Unauthorized"}))
    monkeypatch.setattr(
        mb_cached.session,
        "get",
        lambda url, **kw: FakeResp(
            200,
            {"recordings": [{"id": "rhb", "title": "Song HB", "length": 240000, "artist-credit": [{"name": "Act of Rage"}]}]}
            if url.endswith("/recording")
            else {"id": "rhb", "title": "Song HB", "length": 240000, "genres": [{"name": "hardstyle", "count": 2}]},
        ),
    )
    sum_hb1 = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono_401, "mb": mb_cached},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_hb1.attempted == 1
    t_hb = db.get_track("HB_1")
    assert t_hb.research_status == "success"
    assert t_hb.research_incomplete is False  # Untimed 401 does NOT leave track spinning in queue!

    # Immediate re-run MUST attempt 0 tracks (no infinite loop!) and recover-research classifies HB_1 as ambiguous_hard_blocked!
    sum_hb2 = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono_401, "mb": mb_cached},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_hb2.attempted == 0
    rec_check = db.inspect_or_apply_research_recovery(configured_providers={"sonovault", "musicbrainz"}, apply=False, now_utc=clock.now_utc())
    assert any(it["file_id"] == "HB_1" and it["reason"] == "ambiguous_hard_blocked_provider_audit" for it in rec_check["ambiguous_tracks"])

    # 1b & 2d: Healthy provider continues past a track whose ONLY remaining provider is on cooldown,
    #          AND changing (artist, title) on same file version triggers fresh query instead of reusing old audit!
    _seed_live_track(db, "00_NEEDS_SONO_ONLY", "Artist A - Track A.flac", artist="Artist A", title="Track A", version="v_ta")
    _seed_live_track(db, "ZZ_FRESH_FOR_MB", "Artist B - Track B.flac", artist="Artist B", title="Track B", version="v_tb")

    sono_cd = SonoVaultProvider(
        api_key="sv_cd_1b",
        db=db,
        default_delay=0.0,
        max_retry_sleep_seconds=0.0,
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    monkeypatch.setattr(sono_cd.session, "get", lambda *a, **kw: FakeResp(429, {"error": "Rate limit"}, headers={"Retry-After": "600"}))
    # Trigger SonoVault cooldown on 00_NEEDS_SONO_ONLY while MB completes cleanly on 00_NEEDS_SONO_ONLY (limit=1)
    monkeypatch.setattr(mb_cached.session, "get", lambda *a, **kw: FakeResp(200, {"recordings": []}))
    execute_research_batch(
        cfg,
        db,
        limit=1,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono_cd, "mb": mb_cached},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    # Force 00_NEEDS_SONO_ONLY due right now while SonoVault is STILL on 600s cooldown:
    # Run must skip 00_NEEDS_SONO_ONLY without breaking batch, and process ZZ_FRESH_FOR_MB on healthy MB!
    with db._get_connection() as conn:
        conn.execute("UPDATE tracks SET research_next_retry_utc = '' WHERE file_id = '00_NEEDS_SONO_ONLY'")

    mb_seen_b: List[str] = []

    def _mb_for_b(url: str, params: Optional[Dict[str, Any]] = None, **kw):
        mb_seen_b.append(str((params or {}).get("query") or ""))
        return FakeResp(200, {"recordings": []})

    monkeypatch.setattr(mb_cached.session, "get", _mb_for_b)
    sum_cont = execute_research_batch(
        cfg,
        db,
        limit=5,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono_cd, "mb": mb_cached},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert sum_cont.deferred_without_attempt == 1
    assert sum_cont.attempted == 1  # Continued past 00_NEEDS_SONO_ONLY and researched ZZ_FRESH_FOR_MB!
    assert any("Track B" in q for q in mb_seen_b)

    # 2d: Refine artist/title on 00_NEEDS_SONO_ONLY at the SAME version 'v_ta' -> MB must NOT reuse stale query_title!
    with db._get_connection() as conn:
        conn.execute(
            "UPDATE tracks SET artist = 'Refined Artist', title = 'Refined Title', research_next_retry_utc = '' WHERE file_id = '00_NEEDS_SONO_ONLY'"
        )
    mb_seen_b.clear()
    execute_research_batch(
        cfg,
        db,
        limit=1,
        enable_wikidata_fallback=False,
        providers_override={"sono": sono_cd, "mb": mb_cached},
        clock_utc=clock.now_utc,
        clock_mono=clock.now_mono,
        sleep_fn=clock.sleep,
    )
    assert any("Refined Title" in q for q in mb_seen_b)
