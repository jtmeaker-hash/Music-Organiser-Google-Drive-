"""
Configuration and secrets management for Music Vault Manager.
Stores settings in ~/.config/musicvault/.env with 0600 permissions.
"""
import os
import stat
from pathlib import Path
from typing import Dict, Optional, Any

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "musicvault"
DEFAULT_ENV_FILE = DEFAULT_CONFIG_DIR / ".env"
DEFAULT_DB_FILE = DEFAULT_CONFIG_DIR / "musicvault.db"
DEFAULT_TOKEN_FILE = DEFAULT_CONFIG_DIR / "token.json"
DEFAULT_SECRETS_FILE = DEFAULT_CONFIG_DIR / "client_secrets.json"

DEFAULT_SOURCE_FOLDER_ID = "1m5jXhGCESHVcwXbAgAcbuuAPVI0Dt7j_"
DEFAULT_VAULT_FOLDER_ID = "1qqzURCAV4CYCs1xmYHQv0nZXhmfLCxmi"
DEFAULT_SHEET_ID = "1Dl1UR35uyEC0g7lHKYL1gEbxu7LV2I1t6mHw2QSWUPE"
DEFAULT_CONTACT = "https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-"


def ensure_secure_dir(dir_path: Path) -> None:
    """Ensure directory exists with restricted permissions (0700)."""
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dir_path, stat.S_IRWXU)
    except OSError:
        pass


def mask_secret(value: Optional[str]) -> str:
    """Mask secret value for safe display in logs and reports."""
    if not value:
        return "[Not Set]"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-3:]}"


class Config:
    """Application configuration manager."""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        ensure_secure_dir(self.config_dir)
        self.env_file = self.config_dir / ".env"
        self.db_path = self.config_dir / "musicvault.db"
        self.token_path = self.config_dir / "token.json"
        self.client_secrets_path = self.config_dir / "client_secrets.json"
        self._load()

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

    def save(self) -> None:
        """Persist current configuration to .env with 0600 permissions."""
        ensure_secure_dir(self.config_dir)
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
            "",
        ]
        # Write securely
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

    # Property accessors
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
