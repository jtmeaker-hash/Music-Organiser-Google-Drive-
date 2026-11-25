"""
Interactive credential and configuration wizard.
Provides masked inputs, connectivity testing, and secure .env (0600) storage.
"""
import getpass
import sys
from pathlib import Path
from typing import Optional

from ..config import Config, mask_secret
from ..providers.sonovault import SonoVaultProvider
from ..providers.audiometa import AudioMetaProvider
from ..providers.discogs import DiscogsProvider
from ..auth import GoogleAuthManager


def run_setup_wizard(config: Config) -> None:
    """Run interactive setup wizard to configure API keys and Google authentication."""
    print("=" * 60)
    print("      Music Vault Manager — First-Run Setup Wizard      ")
    print("=" * 60)
    print("Credentials will be securely stored in:")
    print(f"  {config.env_file} (permissions: 0600 - user only)")
    print("You can press ENTER to keep current values or skip optional providers.\n")

    # 1. SonoVault API Key
    curr_sono = config.sonovault_api_key
    print(f"[1/4] SonoVault API Key (Current: {mask_secret(curr_sono)})")
    new_sono = getpass.getpass("Enter SonoVault API Key (or ENTER to keep): ").strip()
    if new_sono:
        config.set("SONOVAULT_API_KEY", new_sono)
        print("Testing SonoVault connectivity...")
        client = SonoVaultProvider(api_key=new_sono)
        try:
            # lightweight test
            client.search("Rebelion", "City")
            print("  [✓] SonoVault connection verified.")
        except Exception as e:
            print(f"  [!] Note: Connection check yielded: {e} (key saved)")

    # 2. AudioMeta API Key
    curr_am = config.audiometa_api_key
    print(f"\n[2/4] AudioMeta API Key (Current: {mask_secret(curr_am)})")
    new_am = getpass.getpass("Enter AudioMeta API Key (or ENTER to keep): ").strip()
    if new_am:
        config.set("AUDIOMETA_API_KEY", new_am)
        print("Testing AudioMeta connectivity...")
        client = AudioMetaProvider(api_key=new_am)
        try:
            client.search("Act of Rage", "Living Fast")
            print("  [✓] AudioMeta connection verified.")
        except Exception as e:
            print(f"  [!] Note: Connection check yielded: {e} (key saved)")

    # 3. Discogs Token
    curr_dc = config.discogs_token
    print(f"\n[3/4] Discogs Personal Access Token (Current: {mask_secret(curr_dc)})")
    new_dc = getpass.getpass("Enter Discogs Token (or ENTER to keep): ").strip()
    if new_dc:
        config.set("DISCOGS_TOKEN", new_dc)
        print("Testing Discogs connectivity...")
        client = DiscogsProvider(token=new_dc)
        try:
            client.search("Headhunterz", "Dragonborn")
            print("  [✓] Discogs connection verified.")
        except Exception as e:
            print(f"  [!] Note: Connection check yielded: {e} (token saved)")

    # 4. Google OAuth Authentication
    print("\n[4/4] Google Drive & Google Sheets Integration")
    auth_mgr = GoogleAuthManager(config)
    creds = auth_mgr.get_credentials(allow_interactive=False)
    if creds and creds.valid:
        print("  [✓] Active Google OAuth token is already configured and valid.")
    else:
        print("  [!] Google OAuth token not found or expired.")
        choice = input("Would you like to authenticate Google OAuth now? [y/N]: ").strip().lower()
        if choice in ('y', 'yes'):
            auth_mgr.authenticate_interactive()

    print("\n" + "=" * 60)
    print("Setup complete! Your settings have been securely saved.")
    print("=" * 60 + "\n")
