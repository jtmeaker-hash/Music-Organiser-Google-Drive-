"""
Stage 0 safety tests: isolated config, gitignore coverage, secret redaction, and network isolation.
"""
import socket
import subprocess
from pathlib import Path
import pytest

from musicvault.config import Config, redact_secrets, mask_secret


def test_config_isolated_to_temp_dir(isolated_musicvault_environment: Path):
    cfg = Config()
    assert cfg.config_dir == isolated_musicvault_environment
    assert "/root/.config/musicvault" not in str(cfg.db_path)
    assert cfg.dry_run is True
    assert cfg.allow_provisional_moves is False
    assert cfg.provider_terms_cleared is False


def test_redact_secrets_scrubs_headers_urls_and_configured_keys(isolated_musicvault_environment: Path):
    cfg = Config()
    cfg.set("SONOVAULT_API_KEY", "sv_live_987654321secret")
    cfg.set("AUDIOMETA_API_KEY", "amsk_super_secret_token_123")
    cfg.set("DISCOGS_TOKEN", "dc_pat_abcdef0123456789")

    raw_msg = (
        "Failed GET https://example.test/api?api_key=url_secret_999&token=tok_abc "
        "Authorization: Bearer amsk_super_secret_token_123 | "
        "Authorization: Discogs token=dc_pat_abcdef0123456789 | "
        "x-api-key: sv_live_987654321secret | "
        "client_secret=GOCSPX-1234567890abcdef | oauth=ya29.a0AfH6SMBx123456789"
    )
    scrubbed = cfg.redact(raw_msg)
    assert "sv_live_987654321secret" not in scrubbed
    assert "amsk_super_secret_token_123" not in scrubbed
    assert "dc_pat_abcdef0123456789" not in scrubbed
    assert "url_secret_999" not in scrubbed
    assert "tok_abc" not in scrubbed
    assert "GOCSPX-1234567890abcdef" not in scrubbed
    assert "ya29.a0AfH6SMBx123456789" not in scrubbed
    assert "<REDACTED" in scrubbed
    assert "sv_live" not in mask_secret("sv_live_987654321secret")


def test_gitignore_covers_sensitive_and_generated_files():
    repo_root = Path(__file__).resolve().parent.parent
    probe_paths = [
        ".env",
        ".env.production",
        "client_secrets.json",
        "token.json",
        "musicvault.db",
        "musicvault.db-wal",
        "musicvault.db.bak",
        "musicvault.db.backup_20261125.sqlite",
        "sample.wav",
        "sample.flac",
        "debug.log",
        "live_preview.csv",
        "status_report.json",
    ]
    res = subprocess.run(
        ["git", "check-ignore", *probe_paths],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = set(res.stdout.splitlines())
    for p in probe_paths:
        assert p in ignored, f"Expected {p} to be gitignored"


def test_external_network_blocked_in_unit_tests():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="Blocked real external network"):
            s.connect(("8.8.8.8", 443))
    finally:
        s.close()
