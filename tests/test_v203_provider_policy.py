from datetime import datetime, timedelta, timezone

from musicvault.config import Config
from musicvault.core.provider_policy import ProviderPolicy, RequestMonitor
from musicvault.db import Database
from musicvault.providers.base import BaseProvider


def test_quota_unknown_persists_and_credential_change_reopens(tmp_path):
    db = Database(tmp_path / "library.db")
    provider = BaseProvider("SonoVault", db=db)
    provider.register_secret("first", is_primary_credential=True)
    db.set_provider_operational_state(
        "sonovault", last_status="quota_exceeded", cooldown_until_utc="",
        cred_fingerprint=provider._cred_fingerprint,
    )
    same = BaseProvider("SonoVault", db=db)
    same.register_secret("first", is_primary_credential=True)
    assert not same.can_request()
    assert not same._precheck_unavailable().retry_until_utc
    same.register_secret("second", is_primary_credential=True)
    assert same.can_request()


def test_timed_quota_can_expire_after_diagnostics(tmp_path):
    db = Database(tmp_path / "library.db")
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    deadline = now + timedelta(minutes=1)
    db.set_provider_operational_state(
        "sonovault", last_status="quota_exceeded", cooldown_until_utc=deadline.isoformat(),
    )
    provider = BaseProvider("SonoVault", db=db, clock_utc=lambda: now, clock_mono=lambda: now.timestamp())
    assert not provider.can_request()
    now = deadline + timedelta(seconds=1)
    assert provider.can_request()


def test_breaker_restores_and_recovers(tmp_path):
    db = Database(tmp_path / "library.db")
    now = datetime.now(timezone.utc)
    monitor = RequestMonitor(db, "musicbrainz", {})
    assert [monitor.observe("rate_limits", .3, now, 600) for _ in range(3)] == [0, 0, 600]
    options = ProviderPolicy(Config(tmp_path))
    provider = BaseProvider("MusicBrainz", db=db)
    options.bind(db, {"mb": provider})
    assert not provider.can_request()
    assert monitor.observe("successful_responses", .2, now + timedelta(seconds=601)) == 0
    with db._get_connection() as conn:
        assert conn.execute("SELECT level FROM provider_breakers").fetchone()[0] == 0


def test_premium_policy_falls_back_only_for_needed_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TERMS_CLEARED", "true")
    policy = ProviderPolicy(Config(tmp_path))
    policy.bind(Database(tmp_path / "library.db"), {})
    assert policy.reason_to_skip("sonovault", set()) == "sufficient_evidence"
    assert policy.reason_to_skip("sonovault", {"bpm"}) == ""
    assert policy.reason_to_skip("musicbrainz", set()) == ""


def test_sustained_rate_limits_only_suspend_affected_provider(tmp_path, monkeypatch):
    import requests

    db = Database(tmp_path / "library.db")
    policy = ProviderPolicy(Config(tmp_path))
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    tick = 0.0

    def sleep(delay):
        nonlocal tick
        tick += delay

    def provider(name):
        return BaseProvider(name, db=db, clock_mono=lambda: tick, sleep_fn=sleep,
                            clock_utc=lambda: now + timedelta(seconds=tick))

    mb, audio, healthy = map(provider, ("MusicBrainz", "AudioMeta", "Discogs"))
    policy.bind(db, {"mb": mb, "am": audio, "dc": healthy})

    def response(code, text):
        result = requests.Response()
        result.status_code = code
        result._content = text.encode()
        result.headers["Retry-After"] = "1"
        return result

    monkeypatch.setattr(mb.session, "get", lambda *args, **kwargs: response(503, "rate limit exceeded"))
    monkeypatch.setattr(audio.session, "get", lambda *args, **kwargs: response(429, "{}"))
    monkeypatch.setattr(healthy.session, "get", lambda *args, **kwargs: response(200, "{}"))
    assert mb.request_detailed("https://example.test", use_cache=False).request_made
    assert audio.request_detailed("https://example.test", use_cache=False).request_made
    assert not mb.can_request() and not audio.can_request()
    assert healthy.request_detailed("https://example.test", use_cache=False).request_made
    assert healthy.can_request()
    with db._get_connection() as conn:
        stored = dict(conn.execute("SELECT provider,level FROM provider_breakers"))
    assert stored["musicbrainz"] == stored["audiometa"] == 1
    assert stored["discogs"] == 0


def test_provider_checkpoint_survives_interruption_without_requery(tmp_path, monkeypatch):
    from musicvault.core.researcher import execute_research_batch
    from musicvault.models import Candidate, Track, TrackMetadata
    from musicvault.providers.base import ProviderCallOutcome, ProviderStatus

    monkeypatch.setenv("PROVIDER_TERMS_CLEARED", "true")
    config = Config(tmp_path)
    db = Database(config.db_path)
    db.save_track(Track("library-id", "Artist - Release.flac", is_live_source=True,
                        metadata=TrackMetadata(artist="Artist", title="Release", duration=170, version="v1")))
    calls = []

    class MusicBrainz:
        def can_request(self):
            return True

        def search_detailed(self, artist, title, **_kwargs):
            calls.append("musicbrainz")
            return [Candidate("MusicBrainz", title, [artist], duration=170)], ProviderCallOutcome(
                "MusicBrainz", ProviderStatus.OK, request_made=True,
            )

    class Discogs:
        def can_request(self):
            return True

        def search_detailed(self, artist, title):
            calls.append("discogs")
            if calls.count("discogs") == 1:
                raise KeyboardInterrupt
            return [], ProviderCallOutcome("Discogs", ProviderStatus.EMPTY, request_made=True)

    providers = {"mb": MusicBrainz(), "dc": Discogs()}
    policy = ProviderPolicy(config)
    policy.bind(db, providers)
    summary = execute_research_batch(config, db, providers_override=providers, policy=policy,
                                     selected_tracks=[db.get_track("library-id")], enable_wikidata_fallback=False)
    assert summary.interrupted
    assert db.get_candidates("library-id")[0].verification == "Duration supported"
    summary = execute_research_batch(config, db, providers_override=providers, policy=policy,
                                     selected_tracks=[db.get_track("library-id")], enable_wikidata_fallback=False)
    assert summary.completed_tracks == 1
    assert calls == ["musicbrainz", "discogs", "discogs"]
