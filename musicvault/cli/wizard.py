"""
Interactive credential and configuration wizard.
Provides masked inputs, honest HTTP status authentication verification, and secure .env (0600) storage.
"""
import getpass
from typing import Any

from ..auth import GoogleAuthManager
from ..config import Config, mask_secret
from ..providers.audiometa import AudioMetaProvider
from ..providers.base import ProviderCallOutcome, ProviderStatus
from ..providers.discogs import DiscogsProvider
from ..providers.sonovault import SonoVaultProvider


def format_provider_probe_status(outcome: ProviderCallOutcome) -> str:
    """Format an honest, redacted connectivity/authentication status line."""
    code_str = f"HTTP {outcome.http_status}" if outcome.http_status else outcome.status.value
    if outcome.status == ProviderStatus.OK:
        return f"[✓] Authenticated & verified ({code_str}; sample catalogue match returned)"
    if outcome.status == ProviderStatus.EMPTY:
        return f"[✓] Authenticated ({code_str}; valid schema, 0 sample hits — real track coverage unverified)"
    if outcome.status == ProviderStatus.AUTH_ERROR:
        return f"[✗] Authentication/permission failed ({code_str}) — verify API key/token"
    if outcome.status == ProviderStatus.QUOTA_EXCEEDED:
        return f"[!] Quota exceeded ({code_str}) — key recognized but plan quota reached"
    if outcome.status == ProviderStatus.RATE_LIMITED:
        return f"[!] Rate-limited ({code_str}) — wait before retrying"
    if outcome.status == ProviderStatus.UNSUPPORTED_ENDPOINT:
        return f"[✗] Endpoint unavailable/unsupported ({code_str})"
    if outcome.status == ProviderStatus.MALFORMED_JSON:
        return f"[✗] Malformed API response ({code_str}) — provider unverified"
    if outcome.status in (ProviderStatus.TIMEOUT, ProviderStatus.NETWORK_ERROR, ProviderStatus.SERVER_ERROR):
        return f"[!] Provider unreachable/unavailable ({code_str}: {outcome.error})"
    return f"[!] Unverified ({outcome.status.value})"


def run_setup_wizard(config: Config) -> None:
    """Run interactive setup wizard to configure API keys and Google authentication."""
    print("=" * 60)
    print("      Music Vault Manager — Credential & Auth Wizard      ")
    print("=" * 60)
    print("Credentials are stored locally with user-only (0600) permissions:")
    print(f"  {config.env_file}")
    print("Press ENTER to keep existing credentials or skip optional providers.\n")

    # 1. SonoVault API Key
    curr_sono = config.sonovault_api_key
    print(f"[1/4] SonoVault API Key (Current: {mask_secret(curr_sono)})")
    new_sono = getpass.getpass("Enter SonoVault API Key (or ENTER to keep): ").strip()
    target_sono = new_sono or curr_sono
    if new_sono:
        config.set("SONOVAULT_API_KEY", new_sono)
    if target_sono:
        print("  Probing SonoVault API status...")
        client = SonoVaultProvider(api_key=target_sono, contact=config.contact, default_delay=0.0)
        print("  " + config.redact(format_provider_probe_status(client.verify_connectivity())))

    # 2. AudioMeta API Key
    curr_am = config.audiometa_api_key
    print(f"\n[2/4] AudioMeta API Key (Current: {mask_secret(curr_am)})")
    new_am = getpass.getpass("Enter AudioMeta API Key (or ENTER to keep): ").strip()
    target_am = new_am or curr_am
    if new_am:
        config.set("AUDIOMETA_API_KEY", new_am)
    if target_am:
        print("  Probing AudioMeta API status...")
        client_am = AudioMetaProvider(api_key=target_am, contact=config.contact, default_delay=0.0)
        print("  " + config.redact(format_provider_probe_status(client_am.verify_connectivity())))

    # 3. Discogs Token
    curr_dc = config.discogs_token
    print(f"\n[3/4] Discogs Personal Access Token (Current: {mask_secret(curr_dc)})")
    new_dc = getpass.getpass("Enter Discogs Token (or ENTER to keep): ").strip()
    target_dc = new_dc or curr_dc
    if new_dc:
        config.set("DISCOGS_TOKEN", new_dc)
    if target_dc:
        print("  Probing Discogs API status...")
        client_dc = DiscogsProvider(token=target_dc, contact=config.contact, default_delay=0.0)
        print("  " + config.redact(format_provider_probe_status(client_dc.verify_connectivity())))

    # 4. Google OAuth Authentication (reuses existing valid/refreshable token without forcing re-auth!)
    print("\n[4/4] Google Drive & Google Sheets Integration")
    auth_mgr = GoogleAuthManager(config)
    creds = auth_mgr.get_credentials(allow_interactive=False)
    if creds and creds.valid:
        print("  [✓] Existing Google OAuth token is valid and active (reused without re-auth).")
    else:
        print("  [!] Google OAuth token not found, expired, or revoked.")
        choice = input("Would you like to run Google OAuth login now? [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            auth_mgr.authenticate_interactive()

    print("\n" + "=" * 60)
    print("Setup finished. Existing safety settings preserved (DRY_RUN=%s)." % config.dry_run)
    print("=" * 60 + "\n")
