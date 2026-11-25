"""
Configuration and secrets management for Music Vault Manager.
Stores settings in ~/.config/musicvault/.env with 0600 permissions.
"""
import os
import re
import stat
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Any

DEFAULT_SOURCE_FOLDER_ID = "1m5jXhGCESHVcwXbAgAcbuuAPVI0Dt7j_"
DEFAULT_VAULT_FOLDER_ID = "1qqzURCAV4CYCs1xmYHQv0nZXhmfLCxmi"
DEFAULT_SHEET_ID = "1Dl1UR35uyEC0g7lHKYL1gEbxu7LV2I1t6mHw2QSWUPE"
DEFAULT_CONTACT = "https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-"


def resolve_default_config_dir() -> Path:
    """Resolve configuration directory, honoring MUSICVAULT_CONFIG_DIR if set."""
    override = os.environ.get("MUSICVAULT_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "musicvault"


def ensure_secure_dir(dir_path: Path) -> None:
    """Ensure directory exists with restricted permissions (0700)."""
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dir_path, stat.S_IRWXU)
    except OSError:
        pass


def mask_secret(value: Optional[str]) -> str:
    """Mask secret value for safe interactive wizard hints (never logs full secret)."""
    if not value:
        return "[Not Set]"
    if len(value) <= 8:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


_REDACTION_PATTERNS = [
    (re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s\"',;]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(Authorization\s*:\s*Discogs\s+token=)[^\s\"',;]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s\"',;]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(\b(?:Bearer|Discogs\s+token=)\s*)[^\s\"',;]+"), r"\1<REDACTED>"),
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|key|token|access_token|refresh_token|client_secret)=)[^&\s\"']+"
        ),
        r"\1<REDACTED>",
    ),
    (
        re.compile(
            r"(?i)(\b(?:SONOVAULT_API_KEY|AUDIOMETA_API_KEY|DISCOGS_TOKEN|client_secret|refresh_token|access_token)\s*[:=]\s*[\"']?)[^\"'\s,;]+"
        ),
        r"\1<REDACTED>",
    ),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{8,}\b"), "<REDACTED_GOOGLE_SECRET>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "<REDACTED_GOOGLE_KEY>"),
    (re.compile(r"\bamsk_[A-Za-z0-9_-]{8,}\b"), "<REDACTED_AUDIOMETA_KEY>"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{12,}\b"), "<REDACTED_OAUTH_TOKEN>"),
]


def redact_secrets(text: Any, extra_secrets: Iterable[Optional[str]] = ()) -> str:
    """Redact credentials, auth headers, URL tokens, and known secret values from text."""
    if text is None:
        return ""
    out = str(text)
    for secret in extra_secrets:
        if secret and len(secret.strip()) >= 4:
            out = out.replace(secret.strip(), "<REDACTED>")
    for pattern, repl in _REDACTION_PATTERNS:
        out = pattern.sub(repl, out)
    return out


class Config:
    """Application configuration manager."""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = Path(config_dir).expanduser() if config_dir else resolve_default_config_dir()
        ensure_secure_dir(self.config_dir)
        self.env_file = self.config_dir / ".env"
        self.db_path = self.config_dir / "musicvault.db"
        self.token_path = self.config_dir / "token.json"
        self._load()
        custom_secrets = self.get("GOOGLE_CLIENT_SECRETS_FILE")
        if custom_secrets:
            candidate_path = Path(custom_secrets).expanduser()
            self.client_secrets_path = candidate_path
        else:
            self.client_secrets_path = self.config_dir / "client_secrets.json"

    def _load(self) -> None:
        """Load settings from .env file, with environment variable overrides."""
        self._data: Dict[str, str] = {}
        if self.env_file.exists():
            with self.env_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        self._data[k.strip()] = v.strip().strip("'\"")

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get configuration value, checking OS environment first."""
        if key in os.environ:
            return os.environ[key]
        return self._data.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value."""
        val = self.get(key)
        if val is None:
            return default
        return val.strip().lower() in ("true", "1", "yes", "on")

    def set(self, key: str, value: str) -> None:
        """Update or set a configuration key and write with 0600 permissions."""
        self._data[key] = value
        self.save()

    def known_secrets(self) -> List[str]:
        """Return non-empty configured secret values strictly for redaction."""
        vals = [
            self.sonovault_api_key,
            self.audiometa_api_key,
            self.discogs_token,
        ]
        return [v for v in vals if v]

    def redact(self, text: Any) -> str:
        """Redact configured secrets and common credential patterns from text."""
        return redact_secrets(text, self.known_secrets())

    def save(self) -> None:
        """Persist current configuration to .env with 0600 permissions, preserving extra keys."""
        ensure_secure_dir(self.config_dir)
        known_keys = {
            "SONOVAULT_API_KEY",
            "AUDIOMETA_API_KEY",
            "DISCOGS_TOKEN",
            "SOURCE_FOLDER_ID",
            "VAULT_FOLDER_ID",
            "SHEET_ID",
            "MUSICBRAINZ_CONTACT",
            "DRY_RUN",
            "ALLOW_PROVISIONAL_MOVES",
            "PROVIDER_TERMS_CLEARED",
            "SAVE_MEDIUM",
        }
        lines = [
            "# Music Vault Manager Configuration",
            "# File permissions restricted to user-only (0600)",
            "",
            "# External Metadata API Keys",
            f"SONOVAULT_API_KEY={self._data.get('SONOVAULT_API_KEY', '')}",
            f"AUDIOMETA_API_KEY={self._data.get('AUDIOMETA_API_KEY', '')}",
            f"DISCOGS_TOKEN={self._data.get('DISCOGS_TOKEN', '')}",
            "",
            "# Google Drive and Sheets IDs",
            f"SOURCE_FOLDER_ID={self._data.get('SOURCE_FOLDER_ID', DEFAULT_SOURCE_FOLDER_ID)}",
            f"VAULT_FOLDER_ID={self._data.get('VAULT_FOLDER_ID', DEFAULT_VAULT_FOLDER_ID)}",
            f"SHEET_ID={self._data.get('SHEET_ID', DEFAULT_SHEET_ID)}",
            f"MUSICBRAINZ_CONTACT={self._data.get('MUSICBRAINZ_CONTACT', DEFAULT_CONTACT)}",
            "",
            "# Safety & Operational Settings",
            f"DRY_RUN={self._data.get('DRY_RUN', 'True')}",
            f"ALLOW_PROVISIONAL_MOVES={self._data.get('ALLOW_PROVISIONAL_MOVES', 'False')}",
            f"PROVIDER_TERMS_CLEARED={self._data.get('PROVIDER_TERMS_CLEARED', 'False')}",
            f"SAVE_MEDIUM={self._data.get('SAVE_MEDIUM', 'True')}",
        ]
        extra_keys = sorted(k for k in self._data.keys() if k not in known_keys)
        if extra_keys:
            lines.append("")
            lines.append("# Additional Settings")
            for k in extra_keys:
                lines.append(f"{k}={self._data[k]}")
        lines.append("")

        temp_file = self.env_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        try:
            os.chmod(temp_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

        temp_file.replace(self.env_file)
        try:
            os.chmod(self.env_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @property
    def sonovault_api_key(self) -> Optional[str]:
        return self.get("SONOVAULT_API_KEY")

    @property
    def audiometa_api_key(self) -> Optional[str]:
        return self.get("AUDIOMETA_API_KEY")

    @property
    def discogs_token(self) -> Optional[str]:
        return self.get("DISCOGS_TOKEN")

    @property
    def source_folder_id(self) -> str:
        return self.get("SOURCE_FOLDER_ID", DEFAULT_SOURCE_FOLDER_ID) or DEFAULT_SOURCE_FOLDER_ID

    @property
    def vault_folder_id(self) -> str:
        return self.get("VAULT_FOLDER_ID", DEFAULT_VAULT_FOLDER_ID) or DEFAULT_VAULT_FOLDER_ID

    @property
    def sheet_id(self) -> str:
        return self.get("SHEET_ID", DEFAULT_SHEET_ID) or DEFAULT_SHEET_ID

    @property
    def contact(self) -> str:
        return self.get("MUSICBRAINZ_CONTACT", DEFAULT_CONTACT) or DEFAULT_CONTACT

    @property
    def dry_run(self) -> bool:
        return self.get_bool("DRY_RUN", True)

    @property
    def allow_provisional_moves(self) -> bool:
        return self.get_bool("ALLOW_PROVISIONAL_MOVES", False)

    @property
    def provider_terms_cleared(self) -> bool:
        return self.get_bool("PROVIDER_TERMS_CLEARED", False)

    @property
    def save_medium(self) -> bool:
        return self.get_bool("SAVE_MEDIUM", True)
