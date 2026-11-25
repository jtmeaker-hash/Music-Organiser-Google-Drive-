"""
Google OAuth2 Authentication Manager for Termux, PRoot, and standalone environments.
Reuses existing ~/.config/musicvault/token.json (0600) without forcing re-authentication,
retains compatible Drive/Sheets scopes, and handles headless/mobile browser fallback cleanly.
"""
import logging
import os
import stat
from typing import Optional
import google.auth
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Config, ensure_secure_dir, redact_secrets

logger = logging.getLogger("musicvault.auth")

# Retained unchanged from v2.0.0 so existing user token.json remains valid without re-consent
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


class GoogleAuthManager:
    """Manages Google OAuth2 credentials and persistent token reuse."""

    def __init__(self, config: Config):
        self.config = config
        self.token_file = config.token_path
        self.client_secrets_file = config.client_secrets_path

    def get_credentials(self, allow_interactive: bool = True) -> Optional[Credentials]:
        """
        Retrieve valid Google credentials, reusing or refreshing existing token.json first.
        Never deletes or overwrites token.json or client_secrets.json on transient errors.
        """
        creds: Optional[Credentials] = None

        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            except Exception as exc:
                logger.warning("Failed to parse existing token file (%s)", redact_secrets(exc, self.config.known_secrets()))
                creds = None

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
                return creds
            except RefreshError as ref_exc:
                msg = redact_secrets(ref_exc, self.config.known_secrets())
                logger.warning("OAuth token refresh rejected (%s)", msg)
                print(
                    "\n[!] Stored Google OAuth refresh token expired or was revoked "
                    "(note: External OAuth apps in 'Testing' mode expire refresh tokens after 7 days)."
                )
                creds = None
            except Exception as exc:
                # Transient network failure during refresh -> do NOT force interactive overwrite if non-interactive
                logger.warning("Transient error refreshing Google OAuth token: %s", redact_secrets(exc, self.config.known_secrets()))
                if not allow_interactive:
                    return None
                creds = None

        if not allow_interactive:
            return None

        return self.authenticate_interactive()

    def authenticate_interactive(self) -> Optional[Credentials]:
        """Run interactive localhost OAuth flow with automatic browser/headless fallback."""
        if not self.client_secrets_file.exists():
            print("\n[!] Google OAuth client_secrets.json not found at:")
            print(f"    {self.client_secrets_file}")
            print("\nTo configure Google Drive & Sheets access:")
            print("1. Open Google Cloud Console -> APIs & Services -> Credentials")
            print("2. Download your OAuth 2.0 Desktop Client JSON")
            print(f"3. Save it to: {self.client_secrets_file} (chmod 600)")
            return None

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_file),
                scopes=SCOPES,
            )
            print("\nInitiating Google OAuth localhost authorization...")
            print("Copy and open the authorization URL below in your browser if it does not open automatically.")
            try:
                creds = flow.run_local_server(port=8080, open_browser=True)
            except OSError:
                # Port 8080 in use or browser helper unavailable in PRoot -> ephemeral port + URL print
                creds = flow.run_local_server(port=0, open_browser=False)
            self._save_token(creds)
            print("[✓] Google OAuth token saved securely (0600).\n")
            return creds
        except KeyboardInterrupt:
            print("\n[!] OAuth login cancelled by user.")
            return None
        except Exception as exc:
            clean_err = redact_secrets(exc, self.config.known_secrets())
            print(f"\n[✗] Google OAuth flow failed: {clean_err}")
            logger.error("OAuth error: %s", clean_err)
            return None

    def _save_token(self, creds: Credentials) -> None:
        """Save authorized token atomically with 0600 permissions."""
        ensure_secure_dir(self.token_file.parent)
        temp_file = self.token_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        try:
            os.chmod(temp_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temp_file.replace(self.token_file)
        try:
            os.chmod(self.token_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
