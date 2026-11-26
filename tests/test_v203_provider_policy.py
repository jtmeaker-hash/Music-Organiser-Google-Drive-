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
