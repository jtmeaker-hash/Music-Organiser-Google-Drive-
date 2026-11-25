"""
Google OAuth2 Authentication Manager for Termux and standalone environments.
Replaces google.colab.auth with persistent token storage in ~/.config/musicvault/token.json (0600).
"""
import os
import stat
import json
import logging
from pathlib import Path
from typing import Optional, Tuple
import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Config, ensure_secure_dir

logger = logging.getLogger("musicvault.auth")

SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]


class GoogleAuthManager:
    """Manages Google OAuth2 credentials and token persistence."""

    def __init__(self, config: Config):
        self.config = config
        self.token_file = config.token_path
        self.client_secrets_file = config.client_secrets_path

    def get_credentials(self, allow_interactive: bool = True) -> Optional[Credentials]:
        """
        Retrieve valid Google credentials, refreshing if necessary.
        If allow_interactive=True and tokens are missing, prompts user for OAuth flow.
        """
        creds: Optional[Credentials] = None

        # Try loading saved token
        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            except Exception as e:
                logger.warning(f"Failed to load existing token: {e}")
                creds = None

        # Check validity and refresh
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
                return creds
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}")
                creds = None

        if creds and creds.valid:
            return creds

        # Fallback to Application Default Credentials (e.g. gcloud / environment)
        try:
            default_creds, _ = google.auth.default(scopes=SCOPES)
            if default_creds:
                return default_creds
        except Exception:
            pass

        if not allow_interactive:
            return None

        # Interactive setup
        return self.authenticate_interactive()

    def authenticate_interactive(self) -> Optional[Credentials]:
        """
        Run interactive OAuth authentication flow.
        """
        if not self.client_secrets_file.exists():
            print("\n[!] Google OAuth client_secrets.json not found at:")
            print(f"    {self.client_secrets_file}")
            print("\nTo enable Google Drive and Google Sheets access:")
            print("1. Go to Google Cloud Console (https://console.cloud.google.com/)")
            print("2. Create an OAuth 2.0 Client ID (Desktop Application)")
            print("3. Download the JSON file and save it as:")
            print(f"   {self.client_secrets_file}")
            return None

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_file),
                scopes=SCOPES
            )
            # Local server flow (port 8080) works natively on Android/Termux
            print("\nInitiating Google OAuth authorization...")
            print("If a browser does not open automatically, visit the URL displayed below.")
            creds = flow.run_local_server(port=8080, open_browser=True)
            self._save_token(creds)
            print("[✓] Google Authentication successful! Token saved securely.\n")
            return creds
        except Exception as e:
            print(f"\n[✗] OAuth flow failed: {e}")
            logger.error(f"OAuth error: {e}")
            return None

    def _save_token(self, creds: Credentials) -> None:
        """Save authorized token with 0600 permissions."""
        ensure_secure_dir(self.token_file.parent)
        temp_file = self.token_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            f.write(creds.to_json())
        try:
            os.chmod(temp_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        temp_file.replace(self.token_file)
        try:
            os.chmod(self.token_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
