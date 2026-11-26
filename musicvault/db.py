"""
Persistent SQLite database, transactional schema migrations, verified online backups,
inventory reconciliation, and checkpoint management for Music Vault Manager.
"""
import json
import os
import sqlite3
import stat
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .core.matcher import extract_version_tokens, norm, parse_artists
from .models import Candidate, Track, TrackMetadata
from .providers.base import canonical_provider_family

SCHEMA_VERSION = 3

_AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".aiff", ".aif",
    ".m4a", ".ogg", ".opus", ".aac",
)


class DatabaseMigrationError(RuntimeError):
    """Raised when database integrity verification, backup, or migration fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_utc(dt: Optional[datetime] = None) -> datetime:
    d = dt or _utc_now()
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return _norm_utc(dt).isoformat()


def _has_audio_extension(name: str) -> bool:
    return Path(name or "").suffix.lower() in _AUDIO_EXTENSIONS


_TRACKS_V2_COLUMNS: List[Tuple[str, str]] = [
    ("mime_type", "TEXT DEFAULT ''"),
    ("record_origin", "TEXT DEFAULT 'unknown'"),
    ("is_live_source", "INTEGER DEFAULT 0"),
    ("quarantined", "INTEGER DEFAULT 0"),
    ("quarantine_reason", "TEXT DEFAULT ''"),
    ("configured_source_id", "TEXT DEFAULT ''"),
    ("observed_parent_id", "TEXT DEFAULT ''"),
    ("scan_generation", "INTEGER DEFAULT 0"),
    ("content_stale", "INTEGER DEFAULT 0"),
    ("raw_artist", "TEXT DEFAULT ''"),
    ("raw_title", "TEXT DEFAULT ''"),
    ("raw_bpm", "TEXT DEFAULT ''"),
    ("raw_key", "TEXT DEFAULT ''"),
    ("raw_genre", "TEXT DEFAULT ''"),
    ("raw_isrc", "TEXT DEFAULT ''"),
    ("identity_confidence", "TEXT DEFAULT ''"),
    ("sheet_artist", "TEXT DEFAULT ''"),
    ("sheet_title", "TEXT DEFAULT ''"),
    ("filename_artist_hint", "TEXT DEFAULT ''"),
    ("filename_title_hint", "TEXT DEFAULT ''"),
    ("filename_remixer_hint", "TEXT DEFAULT ''"),
    ("audio_format", "TEXT DEFAULT ''"),
    ("hydration_status", "TEXT DEFAULT 'pending'"),
    ("hydration_version", "TEXT DEFAULT ''"),
    ("hydration_method", "TEXT DEFAULT ''"),
    ("hydration_error", "TEXT DEFAULT ''"),
    ("hydration_bytes", "INTEGER DEFAULT 0"),
    ("hydration_attempts", "INTEGER DEFAULT 0"),
    ("last_hydrated_utc", "TEXT DEFAULT ''"),
    ("next_hydrate_retry_utc", "TEXT DEFAULT ''"),
    ("research_status", "TEXT DEFAULT 'pending'"),
    ("research_version", "TEXT DEFAULT ''"),
    ("research_attempts", "INTEGER DEFAULT 0"),
    ("research_last_utc", "TEXT DEFAULT ''"),
    ("research_next_retry_utc", "TEXT DEFAULT ''"),
    ("research_error", "TEXT DEFAULT ''"),
    ("provider_calls_json", "TEXT DEFAULT '{}'"),
    ("research_incomplete", "INTEGER DEFAULT 0"),
    ("research_deferrals", "INTEGER DEFAULT 0"),
]

_CANDIDATES_V2_COLUMNS: List[Tuple[str, str]] = [
    ("content_version", "TEXT DEFAULT ''"),
    ("is_stale", "INTEGER DEFAULT 0"),
    ("match_confidence", "TEXT DEFAULT ''"),
    ("source_family", "TEXT DEFAULT ''"),
    ("updated_utc", "TEXT DEFAULT ''"),
]

_RECOVERABLE_PROVIDER_STATUSES = {
    "rate_limited",
    "cooldown_deferred",
    "budget_exhausted",
    "server_error",
    "timeout",
    "network_error",
}

_HARD_BLOCKED_PROVIDER_STATUSES = {
    "auth_error",
    "quota_exceeded",
    "unsupported_endpoint",
    "malformed_json",
}


def _candidate_verification_rank(verification: Optional[str]) -> int:
    v = (verification or "").strip()
    if v == "ISRC supported":
        return 4
    if v == "Duration supported":
        return 3
    if v == "Text only":
        return 2
    if v in ("Title-only review", "Review only (inferred)"):
        return 1
    return 0


def _candidate_edition_identity(
    provider: str,
    source_family: str,
    title: str,
    artists: List[str],
    url: Optional[str] = None,
) -> Tuple[Any, ...]:
    fam = canonical_provider_family(source_family or provider)
    if fam == "wikidata":
        return ("wikidata", (url or "").strip().lower() or norm(title))
    base_t, ver_tokens, remixer = extract_version_tokens(title or "")
    artists_tuple = tuple(sorted(set(parse_artists(", ".join(artists or [])))))
    return (
        fam,
        base_t or norm(title),
        tuple(sorted(ver_tokens)),
        remixer,
        artists_tuple,
    )


class Database:
    """Manages persistent SQLite operations, verified migrations, caching, and checkpoints."""

    def __init__(self, db_path: Path, *, initialize: bool = True):
        self.db_path = Path(db_path)
        self.last_backup_path: Optional[Path] = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_or_migrate()

    def _get_connection(self, isolation_level: Optional[str] = "") -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=isolation_level)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _check_connection_integrity(conn: sqlite3.Connection) -> Tuple[bool, str]:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        msgs = [str(r[0]) for r in rows]
        ok = len(msgs) == 1 and msgs[0].lower() == "ok"
        return ok, "; ".join(msgs)

    def verify_integrity(self) -> Tuple[bool, str]:
        """Run PRAGMA integrity_check on the active database."""
        if not self.db_path.exists():
            return True, "not_created_yet"
        with self._get_connection() as conn:
            return self._check_connection_integrity(conn)

    def create_verified_backup(self, label: str = "backup") -> Path:
        """
        Create a unique timestamped SQLite online backup (including WAL snapshot)
        and verify PRAGMA integrity_check + table row counts on both original and backup.
        Never overwrites any existing backup file.
        """
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            raise DatabaseMigrationError(f"Database file does not exist or is empty: {self.db_path}")

        safe_label = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in label)
        ts = _utc_now().strftime("%Y%m%dT%H%M%S_%fZ")
        base_name = f"{self.db_path.name}.{safe_label}_{ts}.sqlite"
        backup_path = self.db_path.parent / base_name
        suffix_idx = 1
        while backup_path.exists():
            backup_path = self.db_path.parent / f"{self.db_path.name}.{safe_label}_{ts}_{suffix_idx}.sqlite"
            suffix_idx += 1

        src_conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            src_ok, src_msg = self._check_connection_integrity(src_conn)
            if not src_ok:
                raise DatabaseMigrationError(
                    f"Original database failed PRAGMA integrity_check ({src_msg}); aborting before migration."
                )

            src_tables = [
                r[0]
                for r in src_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            src_counts = {
                tbl: src_conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
                for tbl in src_tables
            }

            dst_conn = sqlite3.connect(str(backup_path), timeout=30.0)
            src_conn.backup(dst_conn)
            dst_conn.commit()

            try:
                os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

            dst_ok, dst_msg = self._check_connection_integrity(dst_conn)
            if not dst_ok:
                raise DatabaseMigrationError(
                    f"Backup database failed PRAGMA integrity_check ({dst_msg}); aborting."
                )

            for tbl, expected_cnt in src_counts.items():
                actual_cnt = dst_conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
                if actual_cnt != expected_cnt:
                    raise DatabaseMigrationError(
                        f"Backup verification mismatch on table '{tbl}': expected {expected_cnt}, got {actual_cnt}"
                    )
        except Exception:
            if dst_conn is not None:
                dst_conn.close()
                dst_conn = None
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if dst_conn is not None:
                dst_conn.close()
            src_conn.close()

        self.last_backup_path = backup_path
        return backup_path

    @staticmethod
    def restore_verified_backup(backup_path: Path, target_db_path: Path) -> Path:
        """
        Safely restore a verified backup after moving the current DB and any WAL/SHM/journal
        sidecars aside for forensic preservation. Never deletes backups or overwrites active files.
        Returns the forensic archive path of the displaced database.
        """
        backup_path = Path(backup_path)
        target_db_path = Path(target_db_path)
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            raise DatabaseMigrationError(f"Backup file does not exist or is empty: {backup_path}")

        with sqlite3.connect(str(backup_path), timeout=30.0) as bconn:
            ok, msg = Database._check_connection_integrity(bconn)
            if not ok:
                raise DatabaseMigrationError(f"Refusing to restore corrupt backup ({msg}): {backup_path}")

        ts = _utc_now().strftime("%Y%m%dT%H%M%S_%fZ")
        forensic_base = target_db_path.parent / f"{target_db_path.name}.forensic_{ts}.sqlite"
        idx = 1
        while forensic_base.exists():
            forensic_base = target_db_path.parent / f"{target_db_path.name}.forensic_{ts}_{idx}.sqlite"
            idx += 1

        if target_db_path.exists():
            target_db_path.rename(forensic_base)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(target_db_path) + suffix)
            if sidecar.exists():
                sidecar.rename(Path(str(forensic_base) + suffix))

        with sqlite3.connect(str(backup_path), timeout=30.0) as src, sqlite3.connect(str(target_db_path), timeout=30.0) as dst:
            src.backup(dst)
            dst.commit()
            ok, msg = Database._check_connection_integrity(dst)
            if not ok:
                raise DatabaseMigrationError(f"Restored database failed integrity check ({msg})")

        try:
            os.chmod(target_db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return forensic_base

    def get_schema_version(self) -> int:
        """Return applied schema version (0 if uninitialized, 1 if legacy v2.0.0, 2+ if migrated)."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return 0
        with self._get_connection() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "schema_migrations" in tables:
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            if "tracks" in tables:
                return 1
            return 0

    def _ensure_v3_tables(self, conn: sqlite3.Connection) -> None:
        for statement in (
            """CREATE TABLE IF NOT EXISTS root_membership (
                role TEXT NOT NULL, root_id TEXT NOT NULL, file_id TEXT NOT NULL,
                parent_id TEXT NOT NULL, path_json TEXT NOT NULL, scan_id INTEGER NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (role, root_id, file_id)
            )""",
            """CREATE INDEX IF NOT EXISTS root_membership_file
               ON root_membership(file_id, role, root_id)""",
            """CREATE TABLE IF NOT EXISTS root_observation (
                scan_id INTEGER NOT NULL, file_id TEXT NOT NULL, item_json TEXT NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scan_id, file_id)
            )""",
            """CREATE TABLE IF NOT EXISTS process_runs (
                id INTEGER PRIMARY KEY, selection TEXT NOT NULL, phase TEXT NOT NULL,
                state_json TEXT NOT NULL, updated_utc TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS process_work (
                run_id INTEGER NOT NULL, file_id TEXT NOT NULL, version TEXT NOT NULL,
                hydrated INTEGER NOT NULL DEFAULT 0, researched INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, file_id)
            )""",
            """CREATE TABLE IF NOT EXISTS enrichment_work (
                file_id TEXT NOT NULL, version TEXT NOT NULL, provider TEXT NOT NULL,
                query_key TEXT NOT NULL, reason TEXT NOT NULL, due_utc TEXT NOT NULL,
                PRIMARY KEY(file_id, version, provider, query_key)
            )""",
            """CREATE TABLE IF NOT EXISTS provider_metrics (
                provider TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
                PRIMARY KEY(provider, metric)
            )""",
            """CREATE TABLE IF NOT EXISTS provider_breakers (
                provider TEXT PRIMARY KEY, history TEXT NOT NULL, streak INTEGER NOT NULL,
                level INTEGER NOT NULL, until_utc TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS scheduler_state (
                name TEXT PRIMARY KEY, value INTEGER NOT NULL
            )""",
        ):
            conn.execute(statement)

    def _seed_source_membership(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            INSERT OR IGNORE INTO root_membership
                (role, root_id, file_id, parent_id, path_json, scan_id)
            SELECT 'source', configured_source_id, file_id,
                COALESCE(NULLIF(observed_parent_id, ''), parent_id, ''), '[]', scan_generation
            FROM tracks WHERE is_live_source = 1 AND quarantined = 0
                AND COALESCE(configured_source_id, '') != ''
        """)

    def _migrate_v2_to_v3(self) -> None:
        conn = self._get_connection(isolation_level=None)
        try:
            # A separate reader can take the online backup while this connection
            # holds the reserved writer lock, including when WAL is active.
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("BEGIN IMMEDIATE")
            backup = self.create_verified_backup("pre_v203_migration")
            self._ensure_auxiliary_tables(conn)
            self._ensure_v3_tables(conn)
            self._seed_source_membership(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, _utc_iso(), backup.name, "multi-root inventory and processing"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except (Exception, KeyboardInterrupt) as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise DatabaseMigrationError(f"Schema v3 migration aborted: {exc}") from exc
        finally:
            conn.close()

    def _init_or_migrate(self) -> None:
        """Initialize fresh v2 database or transactionally migrate v2.0.0 database after backup."""
        current_ver = self.get_schema_version()
        if current_ver > SCHEMA_VERSION:
            raise DatabaseMigrationError("Database schema is newer than this MusicVault build.")
        if current_ver == SCHEMA_VERSION:
            return
        if current_ver == 2:
            self._migrate_v2_to_v3()
            return

        if current_ver == 0:
            self._create_fresh_v2_schema()
            try:
                os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            return

        # Existing legacy v1 (v2.0.0) database -> create verified online backup BEFORE touching DB!
        backup_path = self.create_verified_backup(label="pre_v201_migration")
        self._migrate_v1_to_v2(backup_path)

    def _create_fresh_v2_schema(self) -> None:
        now = _utc_iso()
        extra_track_cols = ",\n                ".join(f"{col} {ddl}" for col, ddl in _TRACKS_V2_COLUMNS)
        extra_cand_cols = ",\n                ".join(f"{col} {ddl}" for col, ddl in _CANDIDATES_V2_COLUMNS)
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"""
            CREATE TABLE IF NOT EXISTS tracks (
                file_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                md5 TEXT DEFAULT '',
                modified_time TEXT DEFAULT '',
                parent_id TEXT DEFAULT '',
                artist TEXT DEFAULT '',
                title TEXT DEFAULT '',
                duration REAL DEFAULT 0.0,
                isrc TEXT DEFAULT '',
                embedded_bpm TEXT DEFAULT '',
                embedded_key TEXT DEFAULT '',
                embedded_genre TEXT DEFAULT '',
                identity_origin TEXT DEFAULT 'embedded',
                version TEXT DEFAULT '',
                last_scanned_utc TEXT DEFAULT '',
                {extra_track_cols}
            )
            """)
            conn.execute(f"""
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                title TEXT DEFAULT '',
                artists_json TEXT DEFAULT '[]',
                duration REAL,
                isrc TEXT,
                bpm REAL,
                key TEXT,
                genre_json TEXT DEFAULT '[]',
                labels_json TEXT DEFAULT '[]',
                url TEXT,
                verification TEXT DEFAULT 'Text only',
                created_utc TEXT NOT NULL,
                {extra_cand_cols},
                FOREIGN KEY (file_id) REFERENCES tracks(file_id)
            )
            """)
            self._ensure_auxiliary_tables(conn)
            self._ensure_v3_tables(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_utc, backup_path, notes) VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, now, "", "initial_v3_schema"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _ensure_auxiliary_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS field_decisions (
            file_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            value TEXT DEFAULT '',
            level TEXT DEFAULT 'LOW',
            state TEXT DEFAULT 'REVIEW',
            reason TEXT DEFAULT '',
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (file_id, field_name),
            FOREIGN KEY (file_id) REFERENCES tracks(file_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_cache (
            provider TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            ttl_seconds INTEGER DEFAULT 86400,
            PRIMARY KEY (provider, cache_key)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rollback_manifests (
            manifest_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            file_id TEXT,
            details_json TEXT NOT NULL,
            created_utc TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_utc TEXT NOT NULL,
            backup_path TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_folder_id TEXT NOT NULL,
            started_utc TEXT NOT NULL,
            completed_utc TEXT DEFAULT '',
            status TEXT NOT NULL,
            seen_count INTEGER DEFAULT 0,
            error_text TEXT DEFAULT ''
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            existing_value TEXT DEFAULT '',
            incoming_value TEXT DEFAULT '',
            existing_source TEXT DEFAULT '',
            incoming_source TEXT DEFAULT '',
            version TEXT DEFAULT '',
            created_utc TEXT NOT NULL,
            resolved INTEGER DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            old_version TEXT DEFAULT '',
            archived_utc TEXT NOT NULL,
            reason TEXT NOT NULL,
            track_snapshot_json TEXT NOT NULL,
            candidates_json TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_operational_state (
            provider TEXT PRIMARY KEY,
            cooldown_until_utc TEXT DEFAULT '',
            last_request_utc TEXT DEFAULT '',
            last_status TEXT DEFAULT '',
            last_http_status INTEGER,
            error_kind TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            cred_fingerprint TEXT DEFAULT '',
            updated_utc TEXT NOT NULL
        )
        """)
        existing_pos_cols = {r["name"] for r in conn.execute("PRAGMA table_info(provider_operational_state)").fetchall()}
        if existing_pos_cols and "cred_fingerprint" not in existing_pos_cols:
            conn.execute("ALTER TABLE provider_operational_state ADD COLUMN cred_fingerprint TEXT DEFAULT ''")
        # Idempotently ensure v2.0.2 additive columns exist on already-migrated v2 databases
        existing_t_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if existing_t_cols:
            for col_name, col_ddl in (
                ("research_incomplete", "INTEGER DEFAULT 0"),
                ("research_deferrals", "INTEGER DEFAULT 0"),
            ):
                if col_name not in existing_t_cols:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_ddl}")
        existing_c_cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
        if existing_c_cols and "updated_utc" not in existing_c_cols:
            conn.execute("ALTER TABLE candidates ADD COLUMN updated_utc TEXT DEFAULT ''")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_file ON candidates(file_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_live_hydration ON tracks(is_live_source, quarantined, hydration_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_live_research ON tracks(is_live_source, quarantined, research_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conflicts_file ON metadata_conflicts(file_id)")

    def _migrate_v1_to_v2(self, backup_path: Path) -> None:
        """Transactionally migrate a v2.0.0 database to schema v2 without dropping or losing records."""
        now = _utc_iso()
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")

            existing_track_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()
            }
            for col, ddl in _TRACKS_V2_COLUMNS:
                if col not in existing_track_cols:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {ddl}")

            existing_cand_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()
            }
            for col, ddl in _CANDIDATES_V2_COLUMNS:
                if col not in existing_cand_cols:
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {ddl}")

            self._ensure_auxiliary_tables(conn)

            # 1. Quarantine blank/invalid file_ids without deleting rows
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'invalid',
                is_live_source = 0,
                quarantined = 1,
                quarantine_reason = 'blank_file_id',
                hydration_status = 'permanent_unsupported',
                research_status = 'permanent_unsupported'
            WHERE TRIM(COALESCE(file_id, '')) = ''
            """)

            # 2. Quarantine legacy checkpoint-only rows (blank parent_id and zero size from import_json_checkpoint)
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'checkpoint_import',
                is_live_source = 0,
                quarantined = 1,
                quarantine_reason = 'legacy_checkpoint_unconfirmed_live'
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND TRIM(COALESCE(parent_id, '')) = ''
              AND COALESCE(size, 0) = 0
            """)

            # 3. Mark rows observed with non-empty parent_id from v2.0.0 scan as live_scan
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'live_scan',
                observed_parent_id = parent_id,
                is_live_source = 1,
                quarantined = 0,
                quarantine_reason = ''
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND TRIM(COALESCE(parent_id, '')) != ''
            """)

            # 4. Remaining non-empty IDs with size > 0 but blank parent: keep unquarantined non-live until scan confirms
            conn.execute("""
            UPDATE tracks
            SET record_origin = CASE WHEN record_origin = 'unknown' THEN 'legacy_cached' ELSE record_origin END
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND record_origin = 'unknown'
            """)

            # 5. Backfill raw_* fields and hydration/identity status
            conn.execute("""
            UPDATE tracks
            SET raw_artist = CASE WHEN raw_artist = '' THEN COALESCE(artist, '') ELSE raw_artist END,
                raw_title = CASE WHEN raw_title = '' THEN COALESCE(title, '') ELSE raw_title END,
                raw_bpm = CASE WHEN raw_bpm = '' THEN COALESCE(embedded_bpm, '') ELSE raw_bpm END,
                raw_key = CASE WHEN raw_key = '' THEN COALESCE(embedded_key, '') ELSE raw_key END,
                raw_genre = CASE WHEN raw_genre = '' THEN COALESCE(embedded_genre, '') ELSE raw_genre END,
                raw_isrc = CASE WHEN raw_isrc = '' THEN COALESCE(isrc, '') ELSE raw_isrc END
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'success',
                hydration_version = COALESCE(version, ''),
                hydration_method = 'legacy_embedded',
                identity_confidence = 'verified_embedded'
            WHERE quarantined = 0
              AND TRIM(COALESCE(artist, '')) != ''
              AND TRIM(COALESCE(title, '')) != ''
              AND COALESCE(identity_origin, 'embedded') = 'embedded'
            """)

            # Checkpoint-imported rows also get accurate metadata status preserved while quarantined from routing
            conn.execute("""
            UPDATE tracks
            SET hydration_status = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '' AND COALESCE(identity_origin, 'embedded') = 'embedded'
                    THEN 'success'
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN 'partial'
                    ELSE 'pending'
                END,
                hydration_version = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN COALESCE(version, '')
                    ELSE ''
                END,
                identity_confidence = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '' AND COALESCE(identity_origin, 'embedded') = 'embedded'
                    THEN 'verified_embedded'
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN 'filename_parsed'
                    ELSE 'unresolved'
                END
            WHERE record_origin = 'checkpoint_import'
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'partial',
                hydration_version = COALESCE(version, ''),
                hydration_method = 'legacy_filename_fallback',
                identity_confidence = 'filename_parsed'
            WHERE quarantined = 0
              AND TRIM(COALESCE(artist, '')) != ''
              AND TRIM(COALESCE(title, '')) != ''
              AND COALESCE(identity_origin, '') != 'embedded'
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'pending',
                identity_confidence = 'unresolved'
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND (TRIM(COALESCE(artist, '')) = '' OR TRIM(COALESCE(title, '')) = '')
              AND hydration_status != 'permanent_unsupported'
            """)

            self._ensure_v3_tables(conn)
            self._seed_source_membership(conn)

            # 6. Backfill candidate content_version and track research_status
            conn.execute("""
            UPDATE candidates
            SET content_version = COALESCE(
                (SELECT t.version FROM tracks t WHERE t.file_id = candidates.file_id),
                ''
            )
            WHERE COALESCE(content_version, '') = ''
            """)

            conn.execute("""
            UPDATE tracks
            SET research_status = 'success',
                research_version = COALESCE(version, '')
            WHERE EXISTS (
                SELECT 1 FROM candidates c WHERE c.file_id = tracks.file_id AND COALESCE(c.is_stale, 0) = 0
            )
            """)

            conn.execute(
                "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                (
                    "SCHEMA_MIGRATION",
                    None,
                    json.dumps({"from_version": 1, "to_version": SCHEMA_VERSION, "backup_file": backup_path.name}),
                    now,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_utc, backup_path, notes) VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, now, backup_path.name, "migrated_from_v2_0_0"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise DatabaseMigrationError(f"Migration to schema v{SCHEMA_VERSION} failed and rolled back: {exc}") from exc
        finally:
            conn.close()

    # --- Row Deserialization ---

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> Track:
        keys = set(row.keys())
        meta = TrackMetadata(
            version=row["version"] or "",
            artist=row["artist"] or "",
            title=row["title"] or "",
            duration=float(row["duration"] or 0.0),
            isrc=row["isrc"] or "",
            embedded_bpm=row["embedded_bpm"] or "",
            embedded_key=row["embedded_key"] or "",
            embedded_genre=row["embedded_genre"] or "",
            identity_origin=row["identity_origin"] or "embedded",
            raw_artist=row["raw_artist"] if "raw_artist" in keys else "",
            raw_title=row["raw_title"] if "raw_title" in keys else "",
            raw_bpm=row["raw_bpm"] if "raw_bpm" in keys else "",
            raw_key=row["raw_key"] if "raw_key" in keys else "",
            raw_genre=row["raw_genre"] if "raw_genre" in keys else "",
            raw_isrc=row["raw_isrc"] if "raw_isrc" in keys else "",
            identity_confidence=row["identity_confidence"] if "identity_confidence" in keys else "",
            sheet_artist=row["sheet_artist"] if "sheet_artist" in keys else "",
            sheet_title=row["sheet_title"] if "sheet_title" in keys else "",
            filename_artist_hint=row["filename_artist_hint"] if "filename_artist_hint" in keys else "",
            filename_title_hint=row["filename_title_hint"] if "filename_title_hint" in keys else "",
            filename_remixer_hint=row["filename_remixer_hint"] if "filename_remixer_hint" in keys else "",
            audio_format=row["audio_format"] if "audio_format" in keys else "",
        )
        return Track(
            file_id=row["file_id"],
            name=row["name"] or "",
            size=int(row["size"] or 0),
            md5=row["md5"] or "",
            modified_time=row["modified_time"] or "",
            parent_id=row["parent_id"] or "",
            metadata=meta,
            mime_type=row["mime_type"] if "mime_type" in keys else "",
            record_origin=row["record_origin"] if "record_origin" in keys else "unknown",
            is_live_source=bool(row["is_live_source"]) if "is_live_source" in keys else False,
            quarantined=bool(row["quarantined"]) if "quarantined" in keys else False,
            quarantine_reason=row["quarantine_reason"] if "quarantine_reason" in keys else "",
            configured_source_id=row["configured_source_id"] if "configured_source_id" in keys else "",
            observed_parent_id=row["observed_parent_id"] if "observed_parent_id" in keys else "",
            scan_generation=int(row["scan_generation"] or 0) if "scan_generation" in keys else 0,
            content_stale=bool(row["content_stale"]) if "content_stale" in keys else False,
            hydration_status=row["hydration_status"] if "hydration_status" in keys else "pending",
            hydration_version=row["hydration_version"] if "hydration_version" in keys else "",
            hydration_method=row["hydration_method"] if "hydration_method" in keys else "",
            hydration_error=row["hydration_error"] if "hydration_error" in keys else "",
            hydration_bytes=int(row["hydration_bytes"] or 0) if "hydration_bytes" in keys else 0,
            hydration_attempts=int(row["hydration_attempts"] or 0) if "hydration_attempts" in keys else 0,
            last_hydrated_utc=row["last_hydrated_utc"] if "last_hydrated_utc" in keys else "",
            next_hydrate_retry_utc=row["next_hydrate_retry_utc"] if "next_hydrate_retry_utc" in keys else "",
            research_status=row["research_status"] if "research_status" in keys else "pending",
            research_version=row["research_version"] if "research_version" in keys else "",
            research_attempts=int(row["research_attempts"] or 0) if "research_attempts" in keys else 0,
            research_last_utc=row["research_last_utc"] if "research_last_utc" in keys else "",
            research_next_retry_utc=row["research_next_retry_utc"] if "research_next_retry_utc" in keys else "",
            research_error=row["research_error"] if "research_error" in keys else "",
            provider_calls_json=row["provider_calls_json"] if "provider_calls_json" in keys else "{}",
            research_incomplete=bool(row["research_incomplete"]) if "research_incomplete" in keys else False,
            research_deferrals=int(row["research_deferrals"] or 0) if "research_deferrals" in keys else 0,
        )

    # --- Conflict & Evidence Archival Helpers ---

    def _record_conflict_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        field_name: str,
        existing_value: Any,
        incoming_value: Any,
        existing_source: str,
        incoming_source: str,
        version: str,
    ) -> None:
        ex_str = str(existing_value or "").strip()
        in_str = str(incoming_value or "").strip()
        if not ex_str or not in_str or ex_str == in_str:
            return
        conn.execute(
            """
            INSERT INTO metadata_conflicts (
                file_id, field_name, existing_value, incoming_value,
                existing_source, incoming_source, version, created_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, field_name, ex_str, in_str, existing_source, incoming_source, version, _utc_iso()),
        )

    def record_conflict(
        self,
        file_id: str,
        field_name: str,
        existing_value: Any,
        incoming_value: Any,
        existing_source: str,
        incoming_source: str,
        version: str = "",
    ) -> None:
        with self._get_connection() as conn:
            self._record_conflict_conn(
                conn, file_id, field_name, existing_value, incoming_value,
                existing_source, incoming_source, version
            )

    def get_conflicts(self, file_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if file_id:
                rows = conn.execute(
                    "SELECT * FROM metadata_conflicts WHERE file_id = ? ORDER BY id ASC", (file_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM metadata_conflicts ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def _archive_stale_version_conn(
        self, conn: sqlite3.Connection, existing_row: sqlite3.Row, reason: str
    ) -> None:
        fid = existing_row["file_id"]
        old_ver = existing_row["version"] or ""
        cands = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0", (fid,)
            ).fetchall()
        ]
        conn.execute(
            """
            INSERT INTO evidence_history (
                file_id, old_version, archived_utc, reason, track_snapshot_json, candidates_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fid, old_ver, _utc_iso(), reason, json.dumps(dict(existing_row)), json.dumps(cands)),
        )
        conn.execute("UPDATE candidates SET is_stale = 1 WHERE file_id = ?", (fid,))

    # --- Checkpoints & Safe Track Persistence ---

    def save_track(self, track: Track) -> None:
        """
        Safely upsert a track record without overwriting existing non-blank metadata with blank values.
        Conflicts between two non-blank values on the same version are logged for review.
        """
        fid = str(track.file_id or "").strip()
        if not fid:
            return

        now = _utc_iso()
        with self._get_connection() as conn:
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()
            meta = track.metadata
            in_ver = meta.version or track.md5 or track.modified_time or ""

            if existing is None:
                has_identity = bool(meta.artist.strip() and meta.title.strip())
                hyd_status = track.hydration_status
                if hyd_status == "pending" and has_identity:
                    hyd_status = "success" if meta.identity_origin == "embedded" else "partial"
                id_conf = meta.identity_confidence
                if not id_conf:
                    if has_identity and meta.identity_origin == "embedded":
                        id_conf = "verified_embedded"
                    elif has_identity:
                        id_conf = "filename_parsed"
                    else:
                        id_conf = "unresolved"

                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        artist, title, duration, isrc, embedded_bpm, embedded_key,
                        embedded_genre, identity_origin, version, last_scanned_utc,
                        mime_type, record_origin, is_live_source, quarantined, quarantine_reason,
                        configured_source_id, observed_parent_id, scan_generation, content_stale,
                        raw_artist, raw_title, raw_bpm, raw_key, raw_genre, raw_isrc,
                        identity_confidence, sheet_artist, sheet_title,
                        filename_artist_hint, filename_title_hint, filename_remixer_hint, audio_format,
                        hydration_status, hydration_version, hydration_method, hydration_error,
                        hydration_bytes, hydration_attempts, last_hydrated_utc, next_hydrate_retry_utc,
                        research_status, research_version, research_attempts, research_last_utc,
                        research_next_retry_utc, research_error, provider_calls_json,
                        research_incomplete, research_deferrals
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?
                    )
                    """,
                    (
                        fid, track.name, track.size, track.md5, track.modified_time, track.parent_id,
                        meta.artist, meta.title, meta.duration, meta.isrc, meta.embedded_bpm, meta.embedded_key,
                        meta.embedded_genre, meta.identity_origin, in_ver, now,
                        track.mime_type, track.record_origin, int(track.is_live_source), int(track.quarantined),
                        track.quarantine_reason, track.configured_source_id,
                        track.observed_parent_id or track.parent_id, track.scan_generation, int(track.content_stale),
                        meta.raw_artist or meta.artist, meta.raw_title or meta.title,
                        meta.raw_bpm or meta.embedded_bpm, meta.raw_key or meta.embedded_key,
                        meta.raw_genre or meta.embedded_genre, meta.raw_isrc or meta.isrc,
                        id_conf, meta.sheet_artist, meta.sheet_title,
                        meta.filename_artist_hint, meta.filename_title_hint, meta.filename_remixer_hint, meta.audio_format,
                        hyd_status, track.hydration_version or (in_ver if has_identity else ""),
                        track.hydration_method, track.hydration_error,
                        track.hydration_bytes, track.hydration_attempts, track.last_hydrated_utc, track.next_hydrate_retry_utc,
                        track.research_status, track.research_version, track.research_attempts, track.research_last_utc,
                        track.research_next_retry_utc, track.research_error, track.provider_calls_json,
                        int(track.research_incomplete), int(track.research_deferrals),
                    ),
                )
                return

            # Existing record: preserve good non-blank values unless content version changed
            ex_ver = existing["version"] or ""
            version_changed = bool(ex_ver and in_ver and ex_ver != in_ver)
            if version_changed:
                self._archive_stale_version_conn(conn, existing, reason="save_track_version_changed")

            def _pick_str(field_col: str, new_val: str, allow_replace_on_new_ver: bool = True) -> str:
                old_val = str(existing[field_col] or "")
                new_clean = str(new_val or "")
                if version_changed and allow_replace_on_new_ver:
                    return new_clean
                if not new_clean:
                    return old_val
                if not old_val:
                    return new_clean
                if old_val != new_clean:
                    # Allow upgrading from filename fallback/hint to embedded without losing conflict audit
                    old_origin = existing["identity_origin"] or ""
                    new_origin = meta.identity_origin or ""
                    self._record_conflict_conn(
                        conn, fid, field_col, old_val, new_clean, old_origin, new_origin, in_ver or ex_ver
                    )
                    if new_origin == "embedded" and old_origin != "embedded":
                        return new_clean
                    return old_val
                return old_val

            merged_name = track.name if (track.name and ( _has_audio_extension(track.name) or not existing["name"] )) else existing["name"]
            merged_size = track.size if track.size > 0 else int(existing["size"] or 0)
            merged_md5 = track.md5 or existing["md5"] or ""
            merged_mod = track.modified_time or existing["modified_time"] or ""
            merged_parent = track.parent_id or existing["parent_id"] or ""

            merged_artist = _pick_str("artist", meta.artist)
            merged_title = _pick_str("title", meta.title)
            merged_dur = (
                meta.duration
                if (version_changed or (meta.duration > 0 and float(existing["duration"] or 0.0) <= 0))
                else (float(existing["duration"] or 0.0) or meta.duration)
            )
            merged_isrc = _pick_str("isrc", meta.isrc)
            merged_bpm = _pick_str("embedded_bpm", meta.embedded_bpm)
            merged_key = _pick_str("embedded_key", meta.embedded_key)
            merged_genre = _pick_str("embedded_genre", meta.embedded_genre)

            merged_origin = meta.identity_origin
            if not version_changed and existing["identity_origin"] == "embedded" and merged_origin != "embedded":
                merged_origin = "embedded"

            conn.execute(
                """
                UPDATE tracks SET
                    name = ?,
                    size = ?,
                    md5 = ?,
                    modified_time = ?,
                    parent_id = ?,
                    artist = ?,
                    title = ?,
                    duration = ?,
                    isrc = ?,
                    embedded_bpm = ?,
                    embedded_key = ?,
                    embedded_genre = ?,
                    identity_origin = ?,
                    version = ?,
                    last_scanned_utc = ?,
                    mime_type = ?,
                    raw_artist = ?,
                    raw_title = ?,
                    raw_bpm = ?,
                    raw_key = ?,
                    raw_genre = ?,
                    raw_isrc = ?,
                    identity_confidence = ?,
                    sheet_artist = ?,
                    sheet_title = ?,
                    filename_artist_hint = ?,
                    filename_title_hint = ?,
                    filename_remixer_hint = ?,
                    audio_format = ?,
                    hydration_status = ?,
                    hydration_version = ?,
                    hydration_method = ?,
                    hydration_error = ?,
                    hydration_bytes = ?,
                    hydration_attempts = ?,
                    last_hydrated_utc = ?,
                    next_hydrate_retry_utc = ?,
                    content_stale = ?
                WHERE file_id = ?
                """,
                (
                    merged_name,
                    merged_size,
                    merged_md5,
                    merged_mod,
                    merged_parent,
                    merged_artist,
                    merged_title,
                    merged_dur,
                    merged_isrc,
                    merged_bpm,
                    merged_key,
                    merged_genre,
                    merged_origin,
                    in_ver or ex_ver,
                    now,
                    track.mime_type or existing["mime_type"] or "",
                    meta.raw_artist or existing["raw_artist"] or merged_artist,
                    meta.raw_title or existing["raw_title"] or merged_title,
                    meta.raw_bpm or existing["raw_bpm"] or merged_bpm,
                    meta.raw_key or existing["raw_key"] or merged_key,
                    meta.raw_genre or existing["raw_genre"] or merged_genre,
                    meta.raw_isrc or existing["raw_isrc"] or merged_isrc,
                    meta.identity_confidence or existing["identity_confidence"] or "",
                    meta.sheet_artist or existing["sheet_artist"] or "",
                    meta.sheet_title or existing["sheet_title"] or "",
                    meta.filename_artist_hint or existing["filename_artist_hint"] or "",
                    meta.filename_title_hint or existing["filename_title_hint"] or "",
                    meta.filename_remixer_hint or existing["filename_remixer_hint"] or "",
                    meta.audio_format or existing["audio_format"] or "",
                    track.hydration_status if track.hydration_status != "pending" or version_changed else existing["hydration_status"],
                    track.hydration_version or existing["hydration_version"] or "",
                    track.hydration_method or existing["hydration_method"] or "",
                    track.hydration_error,
                    track.hydration_bytes if track.hydration_bytes > 0 else int(existing["hydration_bytes"] or 0),
                    track.hydration_attempts if track.hydration_attempts > 0 else int(existing["hydration_attempts"] or 0),
                    track.last_hydrated_utc or existing["last_hydrated_utc"] or "",
                    track.next_hydrate_retry_utc,
                    int(track.content_stale if not version_changed else True),
                    fid,
                ),
            )

    # --- Scan Generation & Live Inventory Reconciliation ---

    def begin_scan_run(self, source_folder_id: str) -> int:
        """Start a tracked inventory scan pass. Membership is finalized only on completion."""
        now = _utc_iso()
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO scan_runs (source_folder_id, started_utc, status, seen_count) VALUES (?, ?, 'in_progress', 0)",
                (source_folder_id, now),
            )
            return int(cur.lastrowid)

    def reconcile_scanned_file(
        self, scan_id: int, source_folder_id: str, item: Dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> str:
        """
        Reconcile a Drive file item by exact file_id without touching tags on unchanged files,
        repairing incomplete unchanged records, and archiving stale evidence on version change.
        """
        fid = str(item.get("id") or "").strip()
        now = _utc_iso()
        if not fid:
            with (nullcontext(connection) if connection is not None else self._get_connection()) as conn:
                conn.execute(
                    "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                    ("SCAN_BLANK_FILE_ID", None, json.dumps({"name": item.get("name", "")}), now),
                )
            return "skipped_blank_id"

        name = str(item.get("name") or "")
        size = int(item.get("size") or 0)
        md5 = str(item.get("md5Checksum") or "")
        mod = str(item.get("modifiedTime") or "")
        mime = str(item.get("mimeType") or "")
        parents = item.get("parents") or []
        parent = str(item.get("scanned_parent") or (parents[0] if parents else "") or "")
        new_ver = md5 or mod

        with (nullcontext(connection) if connection is not None else self._get_connection()) as conn:
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        version, last_scanned_utc, mime_type,
                        record_origin, is_live_source, quarantined, quarantine_reason,
                        configured_source_id, observed_parent_id, scan_generation,
                        content_stale, identity_origin, identity_confidence,
                        hydration_status, research_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live_scan', 1, 0, '', ?, ?, ?, 0, 'unresolved', 'unresolved', 'pending', 'pending')
                    """,
                    (fid, name, size, md5, mod, parent, new_ver, now, mime, source_folder_id, parent, scan_id),
                )
                return "inserted"

            if int(existing["scan_generation"] or 0) == scan_id:
                conn.execute(
                    "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                    ("SCAN_DUPLICATE_FILE_ID", fid, json.dumps({"parent": parent}), now),
                )
                return "duplicate_in_scan"

            old_origin = existing["record_origin"] or "unknown"
            new_origin = (
                "live_and_checkpoint"
                if old_origin in ("checkpoint_import", "live_and_checkpoint")
                else "live_scan"
            )
            old_ver = existing["version"] or ""
            version_changed = bool(old_ver and new_ver and old_ver != new_ver)

            if version_changed:
                self._archive_stale_version_conn(conn, existing, reason="scan_content_version_changed")
                conn.execute(
                    """
                    UPDATE tracks SET
                        name = ?, size = ?, md5 = ?, modified_time = ?, parent_id = ?,
                        mime_type = ?, version = ?, last_scanned_utc = ?,
                        record_origin = ?, is_live_source = 1, quarantined = 0, quarantine_reason = '',
                        configured_source_id = ?, observed_parent_id = ?, scan_generation = ?,
                        content_stale = 1,
                        artist = '', title = '', duration = 0.0, isrc = '',
                        embedded_bpm = '', embedded_key = '', embedded_genre = '',
                        raw_artist = '', raw_title = '', raw_bpm = '', raw_key = '', raw_genre = '', raw_isrc = '',
                        identity_origin = 'unresolved', identity_confidence = 'unresolved',
                        hydration_status = 'pending', hydration_version = '', hydration_method = '', hydration_error = '',
                        research_status = 'pending', research_version = '', research_error = '',
                        research_attempts = 0, research_incomplete = 0, research_deferrals = 0, research_next_retry_utc = ''
                    WHERE file_id = ?
                    """,
                    (name, size, md5, mod, parent, mime, new_ver, now, new_origin, source_folder_id, parent, scan_id, fid),
                )
                return "version_changed_stale"

            # Same version: refresh inventory attributes independently of tags and repair incomplete rows
            target_ver = new_ver or old_ver
            has_complete_embedded = bool(
                str(existing["artist"] or "").strip()
                and str(existing["title"] or "").strip()
                and float(existing["duration"] or 0.0) > 0
                and (existing["identity_origin"] or "") == "embedded"
            )
            curr_hyd = existing["hydration_status"] or "pending"
            curr_hyd_ver = existing["hydration_version"] or ""
            curr_method = existing["hydration_method"] or ""
            already_stream_inspected = (
                (not target_ver or curr_hyd_ver == target_ver)
                and curr_method.startswith(("embedded_ranged", "embedded_full", "full_download", "preserved_same_version"))
            )
            if has_complete_embedded:
                next_hyd = "success"
                id_conf = existing["identity_confidence"] or "verified_embedded"
            elif curr_hyd == "permanent_unsupported" and (not target_ver or curr_hyd_ver == target_ver):
                next_hyd = "permanent_unsupported"
                id_conf = existing["identity_confidence"] or "unresolved"
            elif curr_hyd in ("partial", "unresolved") and already_stream_inspected:
                # Stream was already inspected at this exact version (e.g. tagless or filename-only WAV) -> do not reset!
                next_hyd = curr_hyd
                id_conf = existing["identity_confidence"] or (
                    "filename_ambiguous" if (existing["artist"] and existing["title"]) else "unresolved"
                )
            else:
                # Schedule hydration repair for unchanged uninspected or legacy fallback-only rows
                next_hyd = "pending" if curr_hyd in ("pending", "partial", "unresolved", "") else curr_hyd
                id_conf = existing["identity_confidence"] or (
                    "filename_parsed" if (existing["artist"] and existing["title"]) else "unresolved"
                )

            conn.execute(
                """
                UPDATE tracks SET
                    name = ?,
                    size = ?,
                    md5 = ?,
                    modified_time = ?,
                    parent_id = ?,
                    mime_type = ?,
                    version = ?,
                    last_scanned_utc = ?,
                    record_origin = ?,
                    is_live_source = 1,
                    quarantined = 0,
                    quarantine_reason = '',
                    configured_source_id = ?,
                    observed_parent_id = ?,
                    scan_generation = ?,
                    hydration_status = ?,
                    hydration_version = CASE WHEN ? = 'success' THEN ? ELSE hydration_version END,
                    identity_confidence = ?
                WHERE file_id = ?
                """,
                (
                    name if name else existing["name"],
                    size if size > 0 else int(existing["size"] or 0),
                    md5 or existing["md5"] or "",
                    mod or existing["modified_time"] or "",
                    parent or existing["parent_id"] or "",
                    mime or existing["mime_type"] or "",
                    new_ver or old_ver,
                    now,
                    new_origin,
                    source_folder_id,
                    parent or existing["observed_parent_id"] or "",
                    scan_id,
                    next_hyd,
                    next_hyd,
                    new_ver or old_ver,
                    id_conf,
                    fid,
                ),
            )
            return (
                "updated_preserved"
                if (has_complete_embedded or (curr_hyd in ("partial", "unresolved") and already_stream_inspected))
                else "updated_needs_hydration"
            )

    def complete_scan_run(
        self, scan_id: int, source_folder_id: str, seen_count: int
    ) -> Dict[str, int]:
        """
        Finalize scan generation transactionally AFTER full inventory walk succeeds.
        Marks previously live records not seen in scan_id as non-live without deleting them,
        and keeps checkpoint-only records quarantined from live routing.
        """
        now = _utc_iso()
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE scan_runs SET status = 'completed', completed_utc = ?, seen_count = ? WHERE scan_id = ?",
                (now, seen_count, scan_id),
            )

            # Unseen tracks that were previously marked live become non-live (e.g. moved out of source)
            cur_absent = conn.execute(
                """
                UPDATE tracks
                SET is_live_source = 0
                WHERE is_live_source = 1 AND scan_generation != ?
                """,
                (scan_id,),
            )
            newly_absent = cur_absent.rowcount

            # Ensure checkpoint-only records not seen in live source remain quarantined
            conn.execute(
                """
                UPDATE tracks
                SET is_live_source = 0,
                    quarantined = 1,
                    quarantine_reason = CASE
                        WHEN quarantine_reason = '' THEN 'checkpoint_only_not_in_source'
                        ELSE quarantine_reason
                    END
                WHERE record_origin = 'checkpoint_import' AND scan_generation != ?
                """,
                (scan_id,),
            )

            live_cnt = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0"
            ).fetchone()[0]
            quar_cnt = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE quarantined = 1"
            ).fetchone()[0]

            conn.execute("COMMIT")
            return {
                "live_source_tracks": int(live_cnt),
                "newly_absent_tracks": int(newly_absent),
                "quarantined_tracks": int(quar_cnt),
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def abort_scan_run(self, scan_id: int, seen_count: int, reason: str, interrupted: bool = False) -> None:
        """Record interrupted/failed scan WITHOUT marking unseen tracks absent."""
        status = "interrupted" if interrupted else "failed"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE scan_runs SET status = ?, completed_utc = ?, seen_count = ?, error_text = ? WHERE scan_id = ?",
                (status, _utc_iso(), seen_count, reason[:500], scan_id),
            )

    # --- Hydration & Research State Updates ---

    def update_hydration_result(
        self,
        file_id: str,
        status: str,
        version: str,
        method: str,
        bytes_read: int = 0,
        error_msg: str = "",
        retry_delay_seconds: int = 0,
        metadata: Optional[TrackMetadata] = None,
    ) -> None:
        """Transactionally persist hydration status, provenance, byte usage, and extracted metadata."""
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        next_retry = _utc_iso(now_dt + timedelta(seconds=retry_delay_seconds)) if retry_delay_seconds > 0 else ""

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
            if existing is None:
                conn.execute("ROLLBACK")
                return

            attempts = int(existing["hydration_attempts"] or 0) + 1
            total_bytes = int(existing["hydration_bytes"] or 0) + max(0, int(bytes_read))

            if metadata is not None:
                # Never overwrite good non-blank values with blank values on same version
                def _merge(col: str, new_v: str, incoming_src: str) -> str:
                    old_v = str(existing[col] or "")
                    new_s = str(new_v or "")
                    if not new_s:
                        return old_v
                    if not old_v:
                        return new_s
                    if old_v != new_s:
                        old_src = existing["identity_origin"] or "existing"
                        self._record_conflict_conn(conn, file_id, col, old_v, new_s, old_src, incoming_src, version)
                        # Embedded tags outrank prior filename hints
                        if incoming_src == "embedded" and old_src != "embedded":
                            return new_s
                        return old_v
                    return old_v

                in_src = metadata.identity_origin or method
                m_artist = _merge("artist", metadata.artist, in_src)
                m_title = _merge("title", metadata.title, in_src)
                m_dur = metadata.duration if metadata.duration > 0 else float(existing["duration"] or 0.0)
                m_isrc = _merge("isrc", metadata.isrc, in_src)
                m_bpm = _merge("embedded_bpm", metadata.embedded_bpm, in_src)
                m_key = _merge("embedded_key", metadata.embedded_key, in_src)
                m_genre = _merge("embedded_genre", metadata.embedded_genre, in_src)
                m_origin = (
                    "embedded"
                    if (in_src == "embedded" or existing["identity_origin"] == "embedded") and (m_artist or m_title)
                    else (metadata.identity_origin or existing["identity_origin"] or "unresolved")
                )
                m_conf = metadata.identity_confidence or existing["identity_confidence"] or "unresolved"

                conn.execute(
                    """
                    UPDATE tracks SET
                        artist = ?, title = ?, duration = ?, isrc = ?,
                        embedded_bpm = ?, embedded_key = ?, embedded_genre = ?,
                        identity_origin = ?, identity_confidence = ?,
                        raw_artist = ?, raw_title = ?, raw_bpm = ?, raw_key = ?, raw_genre = ?, raw_isrc = ?,
                        sheet_artist = ?, sheet_title = ?,
                        filename_artist_hint = ?, filename_title_hint = ?, filename_remixer_hint = ?,
                        audio_format = ?,
                        content_stale = CASE WHEN ? IN ('success', 'partial') THEN 0 ELSE content_stale END,
                        hydration_status = ?, hydration_version = ?, hydration_method = ?,
                        hydration_error = ?, hydration_bytes = ?, hydration_attempts = ?,
                        last_hydrated_utc = ?, next_hydrate_retry_utc = ?
                    WHERE file_id = ?
                    """,
                    (
                        m_artist, m_title, m_dur, m_isrc,
                        m_bpm, m_key, m_genre,
                        m_origin, m_conf,
                        metadata.raw_artist or existing["raw_artist"] or "",
                        metadata.raw_title or existing["raw_title"] or "",
                        metadata.raw_bpm or existing["raw_bpm"] or "",
                        metadata.raw_key or existing["raw_key"] or "",
                        metadata.raw_genre or existing["raw_genre"] or "",
                        metadata.raw_isrc or existing["raw_isrc"] or "",
                        metadata.sheet_artist or existing["sheet_artist"] or "",
                        metadata.sheet_title or existing["sheet_title"] or "",
                        metadata.filename_artist_hint or existing["filename_artist_hint"] or "",
                        metadata.filename_title_hint or existing["filename_title_hint"] or "",
                        metadata.filename_remixer_hint or existing["filename_remixer_hint"] or "",
                        metadata.audio_format or existing["audio_format"] or "",
                        status,
                        status, version, method,
                        error_msg[:500], total_bytes, attempts,
                        now, next_retry,
                        file_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE tracks SET
                        hydration_status = ?, hydration_version = ?, hydration_method = ?,
                        hydration_error = ?, hydration_bytes = ?, hydration_attempts = ?,
                        last_hydrated_utc = ?, next_hydrate_retry_utc = ?
                    WHERE file_id = ?
                    """,
                    (status, version, method, error_msg[:500], total_bytes, attempts, now, next_retry, file_id),
                )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> Candidate:
        keys = set(row.keys())
        return Candidate(
            provider=row["provider"],
            title=row["title"],
            artists=json.loads(row["artists_json"] or "[]"),
            duration=row["duration"],
            isrc=row["isrc"],
            bpm=row["bpm"],
            key=row["key"],
            genre=json.loads(row["genre_json"] or "[]"),
            labels=json.loads(row["labels_json"] or "[]"),
            url=row["url"],
            verification=row["verification"],
            content_version=row["content_version"] if "content_version" in keys else "",
            is_stale=bool(row["is_stale"]) if "is_stale" in keys else False,
            match_confidence=row["match_confidence"] if "match_confidence" in keys else "",
            source_family=row["source_family"] if "source_family" in keys else "",
            created_utc=row["created_utc"] if "created_utc" in keys else "",
            updated_utc=row["updated_utc"] if "updated_utc" in keys else "",
        )

    def _merge_candidates_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        current_version: str,
        incoming_candidates: List[Candidate],
        now_iso: str,
    ) -> List[Candidate]:
        """
        Non-destructively merge incoming current-version candidates with stored active candidates:
        1. Marks stored active rows with mismatched non-empty content_version as is_stale=1 (version isolation).
        2. Rejects incoming candidates with mismatched content_version, Identity mismatch, or Version conflict.
        3. Matches by (content_version, canonical_source_family, compatible_recording_edition_key) + URL/ISRC:
           - Preserves all prior candidates from providers that returned empty, failed transiently, or were skipped.
           - Never lets weaker unverified/inferred incoming evidence downgrade or erase stronger prior evidence.
           - Replaces/enriches matching recording entries on equal-or-stronger authoritative refresh while
             preserving original created_utc and setting updated_utc.
        """
        ver_clean = (current_version or "").strip()
        if ver_clean:
            conn.execute(
                """
                UPDATE candidates
                SET is_stale = 1, updated_utc = ?
                WHERE file_id = ?
                  AND COALESCE(is_stale, 0) = 0
                  AND TRIM(COALESCE(content_version, '')) != ''
                  AND content_version != ?
                """,
                (now_iso, file_id, ver_clean),
            )
            # Backfill blank content_version on active same-track rows
            conn.execute(
                """
                UPDATE candidates
                SET content_version = ?
                WHERE file_id = ?
                  AND COALESCE(is_stale, 0) = 0
                  AND TRIM(COALESCE(content_version, '')) = ''
                """,
                (ver_clean, file_id),
            )

        existing_rows = list(
            conn.execute(
                "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY id ASC",
                (file_id,),
            ).fetchall()
        )

        # Build index of active rows in memory so multiple incoming candidates deduplicate cleanly too
        active_entries: List[Dict[str, Any]] = []
        for r in existing_rows:
            c_obj = self._row_to_candidate(r)
            ed_key = _candidate_edition_identity(
                c_obj.provider, c_obj.source_family, c_obj.title, c_obj.artists, c_obj.url
            )
            active_entries.append({"row_id": int(r["id"]), "cand": c_obj, "edition_key": ed_key})

        for inc in incoming_candidates or []:
            inc_ver = (inc.content_version or ver_clean).strip()
            if ver_clean and inc_ver and inc_ver != ver_clean:
                continue
            if (inc.verification or "") in ("Identity mismatch", "Version conflict"):
                continue

            fam = canonical_provider_family(inc.source_family or inc.provider)
            inc_ed_key = _candidate_edition_identity(inc.provider, fam, inc.title, inc.artists, inc.url)
            inc_url = (inc.url or "").strip().lower()
            inc_isrc = norm(inc.isrc) if inc.isrc else ""
            inc_rank = _candidate_verification_rank(inc.verification)

            matched_entry: Optional[Dict[str, Any]] = None
            for entry in active_entries:
                if entry["edition_key"] != inc_ed_key:
                    continue
                ex_c: Candidate = entry["cand"]
                ex_url = (ex_c.url or "").strip().lower()
                ex_isrc = norm(ex_c.isrc) if ex_c.isrc else ""
                # Distinct non-empty URLs for different catalogue recordings stay distinct unless ISRC matches
                if fam != "wikidata" and inc_url and ex_url and inc_url != ex_url:
                    if not (inc_isrc and ex_isrc and inc_isrc == ex_isrc):
                        continue
                matched_entry = entry
                break

            if matched_entry is None:
                cur = conn.execute(
                    """
                    INSERT INTO candidates (
                        file_id, provider, title, artists_json, duration,
                        isrc, bpm, key, genre_json, labels_json, url, verification,
                        created_utc, content_version, is_stale, match_confidence, source_family, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        file_id,
                        inc.provider,
                        inc.title,
                        json.dumps(inc.artists or []),
                        inc.duration,
                        inc.isrc,
                        inc.bpm,
                        inc.key,
                        json.dumps(list(dict.fromkeys(inc.genre or []))),
                        json.dumps(list(dict.fromkeys(inc.labels or []))),
                        inc.url,
                        inc.verification,
                        inc.created_utc or now_iso,
                        inc_ver,
                        inc.match_confidence or "",
                        fam,
                        now_iso,
                    ),
                )
                stored_c = Candidate(
                    provider=inc.provider,
                    title=inc.title,
                    artists=list(inc.artists or []),
                    duration=inc.duration,
                    isrc=inc.isrc,
                    bpm=inc.bpm,
                    key=inc.key,
                    genre=list(dict.fromkeys(inc.genre or [])),
                    labels=list(dict.fromkeys(inc.labels or [])),
                    url=inc.url,
                    verification=inc.verification,
                    content_version=inc_ver,
                    is_stale=False,
                    match_confidence=inc.match_confidence or "",
                    source_family=fam,
                    created_utc=inc.created_utc or now_iso,
                    updated_utc=now_iso,
                )
                active_entries.append({"row_id": int(cur.lastrowid), "cand": stored_c, "edition_key": inc_ed_key})
                continue

            # Merge onto matched existing candidate without downgrading verification or erasing fields
            ex_c = matched_entry["cand"]
            ex_rank = _candidate_verification_rank(ex_c.verification)

            if inc_rank < ex_rank:
                # Incoming is weaker than stored evidence: preserve stronger verification & all populated fields;
                # only backfill completely missing supplementary fields if incoming is catalogue-matched (rank >= 2).
                if inc_rank >= 2:
                    merged_bpm = ex_c.bpm if ex_c.bpm is not None else inc.bpm
                    merged_key = ex_c.key or inc.key
                    merged_isrc = ex_c.isrc or inc.isrc
                    merged_url = ex_c.url or inc.url
                    merged_genres = list(ex_c.genre) if ex_c.genre else list(dict.fromkeys(inc.genre or []))
                    merged_labels = list(ex_c.labels) if ex_c.labels else list(dict.fromkeys(inc.labels or []))
                    conn.execute(
                        """
                        UPDATE candidates SET
                            isrc = ?, bpm = ?, key = ?, genre_json = ?, labels_json = ?, url = ?, updated_utc = ?
                        WHERE id = ?
                        """,
                        (
                            merged_isrc,
                            merged_bpm,
                            merged_key,
                            json.dumps(merged_genres),
                            json.dumps(merged_labels),
                            merged_url,
                            now_iso,
                            matched_entry["row_id"],
                        ),
                    )
                    ex_c.isrc = merged_isrc
                    ex_c.bpm = merged_bpm
                    ex_c.key = merged_key
                    ex_c.genre = merged_genres
                    ex_c.labels = merged_labels
                    ex_c.url = merged_url
                    ex_c.updated_utc = now_iso
                continue

            # Equal or stronger authoritative refresh: update while preserving non-blank fields omitted by refresh,
            # NEVER carrying over unverified/conflicting fields (ex_rank < 3) onto recording-verified evidence (inc_rank >= 3).
            can_inherit_prior_fields = not (inc_rank >= 3 and ex_rank < 3)
            merged_provider = inc.provider or ex_c.provider
            merged_title = inc.title or ex_c.title
            merged_artists = list(inc.artists) if inc.artists else list(ex_c.artists or [])
            merged_dur = inc.duration if inc.duration is not None else (ex_c.duration if can_inherit_prior_fields else None)
            merged_isrc = inc.isrc or (ex_c.isrc if can_inherit_prior_fields else None)
            merged_bpm = inc.bpm if inc.bpm is not None else (ex_c.bpm if can_inherit_prior_fields else None)
            merged_key = inc.key or (ex_c.key if can_inherit_prior_fields else None)
            if inc_rank > ex_rank:
                merged_genres = (
                    list(dict.fromkeys(inc.genre))
                    if inc.genre
                    else (list(dict.fromkeys(ex_c.genre or [])) if can_inherit_prior_fields else [])
                )
                merged_labels = (
                    list(dict.fromkeys(inc.labels))
                    if inc.labels
                    else (list(dict.fromkeys(ex_c.labels or [])) if can_inherit_prior_fields else [])
                )
            else:
                merged_genres = list(dict.fromkeys((inc.genre or []) + (ex_c.genre or [])))
                merged_labels = list(dict.fromkeys((inc.labels or []) + (ex_c.labels or [])))
            merged_url = inc.url or ex_c.url
            merged_verif = inc.verification if inc_rank >= ex_rank else ex_c.verification
            merged_mconf = inc.match_confidence or (ex_c.match_confidence if can_inherit_prior_fields else "")

            conn.execute(
                """
                UPDATE candidates SET
                    provider = ?, title = ?, artists_json = ?, duration = ?,
                    isrc = ?, bpm = ?, key = ?, genre_json = ?, labels_json = ?,
                    url = ?, verification = ?, content_version = ?,
                    match_confidence = ?, source_family = ?, updated_utc = ?
                WHERE id = ?
                """,
                (
                    merged_provider,
                    merged_title,
                    json.dumps(merged_artists),
                    merged_dur,
                    merged_isrc,
                    merged_bpm,
                    merged_key,
                    json.dumps(merged_genres),
                    json.dumps(merged_labels),
                    merged_url,
                    merged_verif,
                    inc_ver or ex_c.content_version,
                    merged_mconf,
                    fam,
                    now_iso,
                    matched_entry["row_id"],
                ),
            )
            ex_c.provider = merged_provider
            ex_c.title = merged_title
            ex_c.artists = merged_artists
            ex_c.duration = merged_dur
            ex_c.isrc = merged_isrc
            ex_c.bpm = merged_bpm
            ex_c.key = merged_key
            ex_c.genre = merged_genres
            ex_c.labels = merged_labels
            ex_c.url = merged_url
            ex_c.verification = merged_verif
            ex_c.content_version = inc_ver or ex_c.content_version
            ex_c.match_confidence = merged_mconf
            ex_c.source_family = fam
            ex_c.updated_utc = now_iso

        final_rows = conn.execute(
            "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY id ASC",
            (file_id,),
        ).fetchall()
        return [self._row_to_candidate(r) for r in final_rows]

    def update_research_result(
        self,
        file_id: str,
        status: str,
        version: str,
        candidates: Optional[List[Candidate]],
        provider_calls: Dict[str, Any],
        error_msg: str = "",
        retry_delay_seconds: float = 0.0,
        retry_until_utc: str = "",
        preserve_existing_candidates_on_failure: bool = True,
        research_incomplete: bool = False,
        consume_attempt: bool = True,
        increment_deferral: bool = False,
        now_utc: Optional[datetime] = None,
    ) -> List[Candidate]:
        """
        Transactionally persist per-track research status, provider call audit, and non-destructively
        merged candidates. Never shortens server-required retry waits and never consumes track attempts
        for pure cooldown/budget deferrals.
        """
        now_dt = _norm_utc(now_utc)
        now = _utc_iso(now_dt)

        computed_retry_dt: Optional[datetime] = None
        if retry_delay_seconds and float(retry_delay_seconds) > 0.0:
            computed_retry_dt = now_dt + timedelta(seconds=float(retry_delay_seconds))
        if retry_until_utc and retry_until_utc.strip():
            try:
                parsed_ru = _norm_utc(datetime.fromisoformat(retry_until_utc.strip().replace("Z", "+00:00")))
                if computed_retry_dt is None or parsed_ru > computed_retry_dt:
                    computed_retry_dt = parsed_ru
            except ValueError:
                pass
        next_retry = _utc_iso(computed_retry_dt) if computed_retry_dt is not None else ""

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT research_attempts, research_deferrals FROM tracks WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if existing is None:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                return []

            prev_attempts = int(existing["research_attempts"] or 0)
            prev_deferrals = int(existing["research_deferrals"] or 0)
            attempts = prev_attempts + (1 if consume_attempt else 0)
            deferrals = prev_deferrals + (1 if increment_deferral else 0)

            merged_cands = self._merge_candidates_conn(
                conn=conn,
                file_id=file_id,
                current_version=version,
                incoming_candidates=candidates or [],
                now_iso=now,
            )

            conn.execute(
                """
                UPDATE tracks SET
                    research_status = ?,
                    research_version = ?,
                    research_attempts = ?,
                    research_last_utc = ?,
                    research_next_retry_utc = ?,
                    research_error = ?,
                    provider_calls_json = ?,
                    research_incomplete = ?,
                    research_deferrals = ?
                WHERE file_id = ?
                """,
                (
                    status,
                    version,
                    attempts,
                    now,
                    next_retry,
                    error_msg[:500],
                    json.dumps(provider_calls, sort_keys=True),
                    int(bool(research_incomplete)),
                    deferrals,
                    file_id,
                ),
            )
            conn.execute("COMMIT")
            return merged_cands
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_research_deferral(
        self,
        file_id: str,
        retry_until_utc: str,
        provider_calls: Optional[Dict[str, Any]] = None,
        reason: str = "",
        now_utc: Optional[datetime] = None,
    ) -> None:
        """
        Record a pure provider cooldown/budget deferral WITHOUT incrementing research_attempts
        and WITHOUT altering candidates or overwriting stronger existing status.
        """
        now_dt = _norm_utc(now_utc)
        now = _utc_iso(now_dt)

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT research_deferrals, research_next_retry_utc, provider_calls_json, research_error FROM tracks WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                return
            deferrals = int(row["research_deferrals"] or 0) + 1
            next_r = retry_until_utc or row["research_next_retry_utc"] or ""
            p_json = (
                json.dumps(provider_calls, sort_keys=True)
                if provider_calls is not None
                else (row["provider_calls_json"] or "{}")
            )
            err = (reason[:500] if reason else (row["research_error"] or ""))
            conn.execute(
                """
                UPDATE tracks SET
                    research_deferrals = ?,
                    research_next_retry_utc = ?,
                    provider_calls_json = ?,
                    research_error = ?,
                    research_last_utc = ?
                WHERE file_id = ?
                """,
                (deferrals, next_r, p_json, err, now, file_id),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # --- Queries ---

    def get_track(self, file_id: str) -> Optional[Track]:
        """Fetch a track by its exact Google Drive file ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
            return self._row_to_track(row) if row else None

    def get_all_tracks(self, live_only: bool = False) -> List[Track]:
        """Fetch stored tracks (optionally restricted to live non-quarantined source tracks)."""
        with self._get_connection() as conn:
            if live_only:
                rows = conn.execute(
                    "SELECT * FROM tracks WHERE is_live_source = 1 AND quarantined = 0 ORDER BY name ASC, file_id ASC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tracks ORDER BY name ASC, file_id ASC").fetchall()
            return [self._row_to_track(r) for r in rows]

    @staticmethod
    def _work_scope(process_run: Optional[Tuple[int, int]], stage: str) -> str:
        if process_run is None:
            return "is_live_source = 1"
        run_id, epoch = map(int, process_run)
        return f"""EXISTS (
            SELECT 1 FROM process_work w
            WHERE w.run_id={run_id} AND w.file_id=tracks.file_id
              AND w.version=tracks.version AND w.{stage} != {epoch}
        )"""

    def get_live_tracks_for_hydration(
        self,
        limit: int,
        include_retryable: bool = True,
        max_attempts: int = 4,
        force_rehydrate: bool = False,
        use_sheet_uninspected: bool = False,
        process_run: Optional[Tuple[int, int]] = None,
    ) -> List[Track]:
        """
        Select live source tracks needing metadata hydration or retry.
        Excludes tagless/partial tracks whose audio stream was already inspected at the current
        file version unless force_rehydrate=True or sheet identity lookup is newly requested.
        """
        now = _utc_iso()
        live_clause = self._work_scope(process_run, "hydrated")
        with self._get_connection() as conn:
            if force_rehydrate:
                rows = conn.execute(
                    f"""
                    SELECT * FROM tracks
                    WHERE {live_clause}
                      AND quarantined = 0
                      AND hydration_status != 'permanent_unsupported'
                    ORDER BY content_stale DESC, hydration_attempts ASC, file_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [self._row_to_track(r) for r in rows]

            retry_clause = (
                """
                OR (
                    hydration_status = 'retryable_failure'
                    AND hydration_attempts < ?
                    AND (next_hydrate_retry_utc = '' OR next_hydrate_retry_utc <= ?)
                )
                """
                if include_retryable
                else ""
            )
            params: List[Any] = []
            if include_retryable:
                params.extend([max_attempts, now])
            params.extend([1 if use_sheet_uninspected else 0, limit])

            rows = conn.execute(
                f"""
                SELECT * FROM tracks
                WHERE {live_clause}
                  AND quarantined = 0
                  AND hydration_status != 'permanent_unsupported'
                  AND (
                      content_stale = 1
                      OR hydration_status = 'pending'
                      {retry_clause}
                      OR (
                          hydration_status IN ('partial', 'unresolved')
                          AND (
                              (version != '' AND hydration_version != version)
                              OR NOT (
                                  hydration_method LIKE 'embedded_ranged%'
                                  OR hydration_method LIKE 'embedded_full%'
                                  OR hydration_method LIKE 'full_download%'
                                  OR hydration_method = 'preserved_same_version'
                              )
                              OR (? = 1 AND TRIM(COALESCE(sheet_artist, '')) = '' AND TRIM(COALESCE(artist, '')) = '')
                          )
                      )
                  )
                ORDER BY content_stale DESC, hydration_attempts ASC, file_id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def get_live_tracks_for_research(
        self,
        limit: int,
        force_refresh: bool = False,
        include_title_only: bool = False,
        max_attempts: int = 3,
        now_utc: Optional[datetime] = None,
        process_run: Optional[Tuple[int, int]] = None,
        queue_class: str = "",
        enrichment_condition: str = "0",
        only_enrichment: bool = False,
    ) -> List[Track]:
        """
        Select next batch of live, non-stale tracks with searchable identity for online research.
        Includes pending tracks, version-changed tracks, due retryable_failure tracks, and
        incomplete tracks (research_incomplete=1) whose retry deadline has arrived.
        """
        live_clause = self._work_scope(process_run, "researched")
        first_pass = "(research_status = 'pending' AND research_attempts = 0 AND research_deferrals = 0 AND research_last_utc = '')"
        queue_filter = {"first": first_pass, "retry": f"NOT {first_pass}"}.get(queue_class, "1")
        retry_order = "COALESCE(NULLIF(research_last_utc, ''), research_next_retry_utc), " if queue_class == "retry" else ""
        now_dt = _norm_utc(now_utc)
        now = _utc_iso(now_dt)
        with self._get_connection() as conn:
            if include_title_only:
                identity_clause = """
                (
                    (TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')
                    OR (TRIM(COALESCE(filename_title_hint, '')) != '' OR TRIM(COALESCE(title, '')) != '')
                )
                """
            else:
                identity_clause = "(TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')"

            if force_refresh:
                status_clause = "research_status != 'permanent_unsupported'"
                params: Tuple[Any, ...] = (limit,)
            else:
                status_clause = """
                (
                    (research_status = 'pending' AND (research_next_retry_utc = '' OR research_next_retry_utc <= ?))
                    OR (research_version != '' AND version != '' AND research_version != version)
                    OR (
                        research_status = 'retryable_failure'
                        AND research_attempts < ?
                        AND (research_next_retry_utc = '' OR research_next_retry_utc <= ?)
                    )
                    OR (
                        COALESCE(research_incomplete, 0) = 1
                        AND research_status NOT IN ('unresolved', 'permanent_unsupported')
                        AND research_attempts < ?
                        AND (research_next_retry_utc = '' OR research_next_retry_utc <= ?)
                    )
                )
                """
                params = (now, max_attempts, now, max_attempts, now, limit)

            if only_enrichment:
                status_clause, params = "0", (limit,)

            rows = conn.execute(
                f"""
                SELECT * FROM tracks
                WHERE {live_clause}
                  AND quarantined = 0
                  AND content_stale = 0
                  AND {identity_clause}
                  AND ({status_clause} OR ({enrichment_condition}))
                  AND ({queue_filter})
                ORDER BY
                    {retry_order}CASE WHEN research_status = 'pending' THEN 0 ELSE 1 END ASC,
                    research_attempts ASC,
                    COALESCE(research_deferrals, 0) ASC,
                    file_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def get_research_queue_diagnostics(
        self,
        include_title_only: bool = False,
        max_attempts: int = 3,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return exact counts of deferred retries, exhausted tracks, blocked tracks, and earliest next_retry_utc."""
        now_dt = _norm_utc(now_utc)
        now = _utc_iso(now_dt)

        with self._get_connection() as conn:
            if include_title_only:
                id_sql = """
                (
                    (TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')
                    OR (TRIM(COALESCE(filename_title_hint, '')) != '' OR TRIM(COALESCE(title, '')) != '')
                )
                """
            else:
                id_sql = "(TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')"

            rows = conn.execute(
                f"""
                SELECT research_status, research_attempts, research_incomplete, research_next_retry_utc,
                       research_version, version
                FROM tracks
                WHERE is_live_source = 1
                  AND quarantined = 0
                  AND content_stale = 0
                  AND {id_sql}
                  AND research_status != 'permanent_unsupported'
                """
            ).fetchall()

        due_now = 0
        deferred_not_due = 0
        exhausted = 0
        unresolved_blocked = 0
        completed_clean = 0
        future_retries: List[str] = []

        for r in rows:
            st = str(r["research_status"] or "pending")
            att = int(r["research_attempts"] or 0)
            inc = bool(r["research_incomplete"])
            nr = str(r["research_next_retry_utc"] or "").strip()
            ver_mismatch = bool(r["research_version"] and r["version"] and r["research_version"] != r["version"])

            if ver_mismatch:
                due_now += 1
                continue
            if st == "unresolved":
                unresolved_blocked += 1
                continue
            if st == "pending":
                if nr and nr > now:
                    deferred_not_due += 1
                    future_retries.append(nr)
                else:
                    due_now += 1
                continue
            if st == "retryable_failure" or inc:
                if att >= max_attempts:
                    exhausted += 1
                elif nr and nr > now:
                    deferred_not_due += 1
                    future_retries.append(nr)
                else:
                    due_now += 1
                continue
            if st in ("success", "partial", "no_match"):
                completed_clean += 1

        active_cds = self.get_all_provider_operational_states(now_utc=now_dt, active_only=True)
        for p_info in active_cds.values():
            cd_u = str(p_info.get("cooldown_until_utc") or "").strip()
            if cd_u and cd_u > now:
                future_retries.append(cd_u)

        earliest_retry = min(future_retries) if future_retries else ""
        return {
            "due_now": due_now,
            "deferred_retry_not_due": deferred_not_due,
            "exhausted_retry_limit": exhausted,
            "unresolved_blocked": unresolved_blocked,
            "completed_clean": completed_clean,
            "earliest_next_retry_utc": earliest_retry,
            "active_provider_cooldowns": active_cds,
        }

    def import_json_checkpoint(self, json_path: Path) -> int:
        """
        Safely import tracks from a legacy JSON checkpoint file WITHOUT overwriting live inventory records.
        Checkpoint-only IDs are quarantined from live routing until confirmed by a live Drive scan.
        """
        if not json_path.exists():
            return 0
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0

        now = _utc_iso()
        count = 0
        with self._get_connection() as conn:
            for raw_id, item in data.items():
                if not isinstance(item, dict):
                    continue
                fid = str(raw_id or "").strip()
                if not fid:
                    conn.execute(
                        "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                        ("CHECKPOINT_BLANK_ID_SKIPPED", None, json.dumps({"raw_id": str(raw_id)}), now),
                    )
                    continue

                cp_ver = str(item.get("version") or "")
                cp_artist = str(item.get("artist") or "").strip()
                cp_title = str(item.get("title") or "").strip()
                cp_dur = float(item.get("duration") or 0.0)
                cp_isrc = str(item.get("isrc") or "").strip()
                cp_bpm = str(item.get("embedded_bpm") or "").strip()
                cp_key = str(item.get("embedded_key") or "").strip()
                cp_genre = str(item.get("embedded_genre") or "").strip()
                cp_origin = str(item.get("identity_origin") or "embedded")

                existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()
                if existing is not None:
                    # NEVER overwrite live Drive name/parent/size/membership or non-blank fields!
                    ex_ver = existing["version"] or ""
                    same_ver = (not ex_ver) or (not cp_ver) or (ex_ver == cp_ver)
                    if not same_ver:
                        self._record_conflict_conn(
                            conn, fid, "version", ex_ver, cp_ver,
                            existing["record_origin"] or "live", "checkpoint_import", ex_ver
                        )
                        continue

                    updates: Dict[str, Any] = {}
                    for col, val in (
                        ("artist", cp_artist),
                        ("title", cp_title),
                        ("isrc", cp_isrc),
                        ("embedded_bpm", cp_bpm),
                        ("embedded_key", cp_key),
                        ("embedded_genre", cp_genre),
                    ):
                        old_v = str(existing[col] or "").strip()
                        if val and not old_v:
                            updates[col] = val
                        elif val and old_v and val != old_v:
                            self._record_conflict_conn(
                                conn, fid, col, old_v, val,
                                existing["identity_origin"] or "live", "checkpoint_import", ex_ver or cp_ver
                            )

                    if cp_dur > 0 and float(existing["duration"] or 0.0) <= 0:
                        updates["duration"] = cp_dur

                    if updates:
                        new_a = updates.get("artist", existing["artist"] or "")
                        new_t = updates.get("title", existing["title"] or "")
                        if new_a and new_t and cp_origin == "embedded":
                            updates["identity_origin"] = "embedded"
                            updates["identity_confidence"] = "verified_embedded"
                            updates["hydration_status"] = "success"
                            updates["hydration_version"] = ex_ver or cp_ver
                            updates["hydration_method"] = "checkpoint_same_version"
                        if existing["record_origin"] == "live_scan":
                            updates["record_origin"] = "live_and_checkpoint"

                        set_sql = ", ".join(f"{k} = ?" for k in updates.keys())
                        conn.execute(
                            f"UPDATE tracks SET {set_sql} WHERE file_id = ?",
                            (*updates.values(), fid),
                        )
                    count += 1
                    continue

                synth_name = f"{cp_artist} - {cp_title}" if (cp_artist or cp_title) else f"[checkpoint:{fid[:8]}]"
                has_id = bool(cp_artist and cp_title)
                hyd_st = "success" if (has_id and cp_origin == "embedded") else ("partial" if has_id else "pending")
                id_conf = "verified_embedded" if (has_id and cp_origin == "embedded") else ("filename_parsed" if has_id else "unresolved")

                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        artist, title, duration, isrc, embedded_bpm, embedded_key,
                        embedded_genre, identity_origin, version, last_scanned_utc,
                        record_origin, is_live_source, quarantined, quarantine_reason,
                        raw_artist, raw_title, raw_bpm, raw_key, raw_genre, raw_isrc,
                        identity_confidence, hydration_status, hydration_version, hydration_method
                    ) VALUES (
                        ?, ?, 0, ?, '', '',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        'checkpoint_import', 0, 1, 'checkpoint_import_unconfirmed_live',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, 'checkpoint_import'
                    )
                    """,
                    (
                        fid, synth_name, cp_ver,
                        cp_artist, cp_title, cp_dur, cp_isrc, cp_bpm, cp_key,
                        cp_genre, cp_origin, cp_ver, now,
                        cp_artist, cp_title, cp_bpm, cp_key, cp_genre, cp_isrc,
                        id_conf, hyd_st, cp_ver if has_id else "",
                    ),
                )
                count += 1
        return count

    # --- Candidate Management ---

    def save_candidates(self, file_id: str, candidates: List[Candidate], content_version: str = "") -> None:
        """Replace active non-stale candidates for a track while preserving archived stale rows."""
        now = _utc_iso()
        with self._get_connection() as conn:
            ver = content_version
            if not ver:
                tr = conn.execute("SELECT version FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
                ver = (tr["version"] or "") if tr else ""
            conn.execute("DELETE FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0", (file_id,))
            for c in candidates:
                conn.execute(
                    """
                    INSERT INTO candidates (
                        file_id, provider, title, artists_json, duration,
                        isrc, bpm, key, genre_json, labels_json, url, verification,
                        created_utc, content_version, is_stale, match_confidence, source_family
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        file_id, c.provider, c.title, json.dumps(c.artists),
                        c.duration, c.isrc, c.bpm, c.key,
                        json.dumps(c.genre), json.dumps(c.labels),
                        c.url, c.verification,
                        now, c.content_version or ver, c.match_confidence, c.source_family,
                    ),
                )

    def get_candidates(self, file_id: str, include_stale: bool = False) -> List[Candidate]:
        """Fetch stored active candidates for a track (excluding stale candidates by default)."""
        with self._get_connection() as conn:
            if include_stale:
                cur = conn.execute("SELECT * FROM candidates WHERE file_id = ? ORDER BY id ASC", (file_id,))
            else:
                cur = conn.execute(
                    "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY id ASC",
                    (file_id,),
                )
            return [self._row_to_candidate(row) for row in cur.fetchall()]

    # --- Provider Operational Cooldown State & Cache ---

    def get_provider_operational_state(self, provider: str) -> Optional[Dict[str, Any]]:
        """Fetch minimal persisted operational state (cooldown/last_request_utc) for a provider."""
        fam = canonical_provider_family(provider)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM provider_operational_state WHERE provider = ?",
                (fam,),
            ).fetchone()
            return dict(row) if row else None

    def set_provider_operational_state(
        self,
        provider: str,
        cooldown_until_utc: Optional[str] = None,
        last_request_utc: Optional[str] = None,
        last_status: str = "",
        last_http_status: Optional[int] = None,
        error_kind: str = "",
        reason: str = "",
        cred_fingerprint: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Persist minimal operational cooldown/pacing state (never stores restricted API payloads or raw keys)."""
        fam = canonical_provider_family(provider)
        now_dt = _norm_utc(now_utc)
        now = _utc_iso(now_dt)

        with self._get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM provider_operational_state WHERE provider = ?",
                (fam,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO provider_operational_state (
                        provider, cooldown_until_utc, last_request_utc,
                        last_status, last_http_status, error_kind, reason, cred_fingerprint, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fam,
                        cooldown_until_utc or "",
                        last_request_utc or "",
                        last_status or "",
                        last_http_status,
                        error_kind or "",
                        (reason or "")[:300],
                        cred_fingerprint or "",
                        now,
                    ),
                )
            else:
                new_cd = cooldown_until_utc if cooldown_until_utc is not None else (existing["cooldown_until_utc"] or "")
                new_lr = last_request_utc if last_request_utc is not None else (existing["last_request_utc"] or "")
                new_st = last_status if last_status else (existing["last_status"] or "")
                new_http = last_http_status if last_http_status is not None else existing["last_http_status"]
                new_ek = error_kind if cooldown_until_utc is not None or error_kind else (existing["error_kind"] or "")
                new_rsn = (reason[:300] if (cooldown_until_utc is not None or reason) else (existing["reason"] or ""))
                ex_fp = existing["cred_fingerprint"] if "cred_fingerprint" in existing.keys() else ""
                new_fp = cred_fingerprint if cred_fingerprint is not None else (ex_fp or "")
                conn.execute(
                    """
                    UPDATE provider_operational_state SET
                        cooldown_until_utc = ?,
                        last_request_utc = ?,
                        last_status = ?,
                        last_http_status = ?,
                        error_kind = ?,
                        reason = ?,
                        cred_fingerprint = ?,
                        updated_utc = ?
                    WHERE provider = ?
                    """,
                    (new_cd, new_lr, new_st, new_http, new_ek, new_rsn, new_fp, now, fam),
                )

    def get_all_provider_operational_states(
        self,
        now_utc: Optional[datetime] = None,
        active_only: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """Inspect persisted provider cooldowns and remaining wait seconds."""
        now_dt = _norm_utc(now_utc)
        out: Dict[str, Dict[str, Any]] = {}
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM provider_operational_state ORDER BY provider ASC").fetchall()
        for r in rows:
            d = dict(r)
            rem = 0.0
            cd_raw = str(d.get("cooldown_until_utc") or "").strip()
            if cd_raw:
                try:
                    cd_dt = _norm_utc(datetime.fromisoformat(cd_raw.replace("Z", "+00:00")))
                    rem = max(0.0, (cd_dt - now_dt).total_seconds())
                except ValueError:
                    rem = 0.0
            d["remaining_cooldown_seconds"] = round(rem, 1)
            d["cooldown_active"] = rem > 0.0
            if active_only and not d["cooldown_active"]:
                continue
            out[str(d["provider"])] = d
        return out

    def get_cache(
        self,
        provider: str,
        cache_key: str,
        now_utc: Optional[datetime] = None,
    ) -> Optional[Any]:
        """Retrieve cached provider response only if within UTC TTL window."""
        ref_now = _norm_utc(now_utc)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT response_json, created_utc, ttl_seconds FROM provider_cache WHERE provider = ? AND cache_key = ?",
                (provider, cache_key),
            ).fetchone()
            if not row:
                return None
            ttl = int(row["ttl_seconds"] or 0)
            if ttl <= 0:
                return None
            try:
                created_raw = str(row["created_utc"]).replace("Z", "+00:00")
                created_dt = datetime.fromisoformat(created_raw)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if (ref_now - created_dt).total_seconds() > ttl:
                    return None
            except ValueError:
                return None
            return json.loads(row["response_json"])

    def set_cache(
        self,
        provider: str,
        cache_key: str,
        data: Any,
        ttl_seconds: int = 86400,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Cache provider response with UTC timestamp and explicit TTL."""
        if ttl_seconds <= 0:
            return
        ref_now = _norm_utc(now_utc)
        now = _utc_iso(ref_now)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO provider_cache (provider, cache_key, response_json, created_utc, ttl_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (provider, cache_key, json.dumps(data), now, int(ttl_seconds)),
            )

    # --- Selective Dry-Run-First Research Recovery ---

    def inspect_or_apply_research_recovery(
        self,
        configured_providers: Optional[Set[str]] = None,
        apply: bool = False,
        verified_backup_path: Optional[Path] = None,
        create_backup: bool = False,
        max_attempts: int = 3,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Inspect (dry-run default) or transactionally re-queue live, non-quarantined, current-content
        tracks stranded in no_match/partial/success or exhausted attempts due to demonstrable
        transient/cooldown/budget provider failures in provider_calls_json.
        - Reports ambiguous historical records separately without resetting them.
        - Never alters quarantined, non-live, stale, or permanent_unsupported records.
        - Preserves all stored candidates and hydration metadata.
        - Requires a verified SQLite backup (--create-backup or --backup-file) when apply=True.
        - Respects active provider cooldowns and is strictly idempotent.
        """
        now_dt = _norm_utc(now_utc)
        now_iso = _utc_iso(now_dt)
        cfg_families = {canonical_provider_family(p) for p in (configured_providers or set()) if p}

        active_cooldowns = self.get_all_provider_operational_states(now_utc=now_dt, active_only=True)

        counts: Dict[str, int] = {
            "total_records_inspected": 0,
            "excluded_quarantined_or_nonlive": 0,
            "excluded_permanent_unsupported": 0,
            "excluded_stale_or_version_mismatch": 0,
            "excluded_missing_identity": 0,
            "already_queued_or_recovered": 0,
            "clean_completed_no_action": 0,
            "recoverable_total": 0,
            "ambiguous_total": 0,
            "applied_count": 0,
        }
        recoverable_reasons: Dict[str, int] = {}
        ambiguous_reasons: Dict[str, int] = {}
        recoverable_items: List[Dict[str, Any]] = []
        ambiguous_items: List[Dict[str, Any]] = []

        with self._get_connection() as conn:
            all_rows = conn.execute("SELECT * FROM tracks ORDER BY file_id ASC").fetchall()
            cand_counts = {
                r["file_id"]: int(r["cnt"])
                for r in conn.execute(
                    "SELECT file_id, COUNT(*) AS cnt FROM candidates WHERE COALESCE(is_stale, 0) = 0 GROUP BY file_id"
                ).fetchall()
            }
            verified_ids = {
                r["file_id"]
                for r in conn.execute(
                    """
                    SELECT DISTINCT file_id FROM candidates
                    WHERE COALESCE(is_stale, 0) = 0
                      AND verification IN ('ISRC supported', 'Duration supported')
                      AND COALESCE(source_family, '') != 'wikidata'
                    """
                ).fetchall()
            }

        for row in all_rows:
            counts["total_records_inspected"] += 1
            fid = str(row["file_id"] or "")
            if not bool(row["is_live_source"]) or bool(row["quarantined"]):
                counts["excluded_quarantined_or_nonlive"] += 1
                continue
            r_status = str(row["research_status"] or "pending")
            h_status = str(row["hydration_status"] or "pending")
            if r_status == "permanent_unsupported" or h_status == "permanent_unsupported":
                counts["excluded_permanent_unsupported"] += 1
                continue
            r_ver = str(row["research_version"] or "")
            t_ver = str(row["version"] or "")
            if bool(row["content_stale"]) or (r_ver and t_ver and r_ver != t_ver):
                counts["excluded_stale_or_version_mismatch"] += 1
                continue
            artist = str(row["artist"] or "").strip()
            title = str(row["title"] or "").strip()
            if not (artist and title):
                counts["excluded_missing_identity"] += 1
                continue

            attempts = int(row["research_attempts"] or 0)
            incomplete_flag = bool(row["research_incomplete"]) if "research_incomplete" in row.keys() else False

            # Already in normal research queue (pending, due/deferred retryable < max_attempts, or already marked incomplete < max_attempts)
            if (
                r_status == "pending"
                or (r_status == "retryable_failure" and attempts < max_attempts)
                or (incomplete_flag and r_status != "unresolved" and attempts < max_attempts)
            ):
                counts["already_queued_or_recovered"] += 1
                continue

            raw_audit = str(row["provider_calls_json"] or "").strip()
            if not raw_audit or raw_audit == "{}":
                amb_reason = "ambiguous_missing_or_legacy_audit"
                counts["ambiguous_total"] += 1
                ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                ambiguous_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "research_status": r_status,
                        "attempts": attempts,
                        "reason": amb_reason,
                    }
                )
                continue

            try:
                parsed_audit = json.loads(raw_audit)
            except ValueError:
                amb_reason = "ambiguous_corrupt_audit_json"
                counts["ambiguous_total"] += 1
                ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                ambiguous_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "research_status": r_status,
                        "attempts": attempts,
                        "reason": amb_reason,
                    }
                )
                continue

            if not isinstance(parsed_audit, dict):
                amb_reason = "ambiguous_corrupt_audit_json"
                counts["ambiguous_total"] += 1
                ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                ambiguous_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "research_status": r_status,
                        "attempts": attempts,
                        "reason": amb_reason,
                    }
                )
                continue

            failed_transient_fams: List[str] = []
            hard_blocked_fams: List[str] = []
            recorded_clean_fams: Set[str] = set()
            not_configured_fams: Set[str] = set()

            for p_key, p_val in parsed_audit.items():
                if p_key == "_meta" or not isinstance(p_val, dict):
                    continue
                fam = canonical_provider_family(p_key)
                if fam == "wikidata":
                    continue
                st_p = str(p_val.get("status") or "").strip().lower()
                alt_node = p_val.get("alternate_query")
                alt_st = str(alt_node.get("status") or "").strip().lower() if isinstance(alt_node, dict) else ""

                if st_p == "not_configured":
                    not_configured_fams.add(fam)
                    continue

                if (
                    st_p in _RECOVERABLE_PROVIDER_STATUSES
                    or alt_st in (_RECOVERABLE_PROVIDER_STATUSES | {"skipped_cooldown_or_budget"})
                    or (p_val.get("lookup_completed") is False and p_val.get("retryable") is True)
                ):
                    failed_transient_fams.append(fam)
                elif st_p in _HARD_BLOCKED_PROVIDER_STATUSES or alt_st in _HARD_BLOCKED_PROVIDER_STATUSES:
                    hard_blocked_fams.append(fam)
                elif st_p in ("ok", "empty"):
                    recorded_clean_fams.add(fam)

            if failed_transient_fams:
                if r_status == "retryable_failure" and attempts >= max_attempts:
                    rec_reason = "recoverable_exhausted_attempts"
                elif r_status == "no_match":
                    rec_reason = "recoverable_no_match_incomplete_audit"
                elif r_status == "partial":
                    rec_reason = "recoverable_partial_incomplete_audit"
                elif r_status == "success":
                    rec_reason = "recoverable_success_incomplete_audit"
                else:
                    rec_reason = f"recoverable_{r_status}_incomplete_audit"

                # Respect active cooldowns: if all failed providers for this track are cooling down, defer until earliest expiry
                cd_deadlines = [
                    str(active_cooldowns[f]["cooldown_until_utc"])
                    for f in failed_transient_fams
                    if f in active_cooldowns and active_cooldowns[f].get("cooldown_until_utc")
                ]
                next_retry_for_track = (
                    min(cd_deadlines) if (cd_deadlines and len(cd_deadlines) == len(failed_transient_fams)) else ""
                )

                counts["recoverable_total"] += 1
                recoverable_reasons[rec_reason] = recoverable_reasons.get(rec_reason, 0) + 1
                recoverable_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "prior_status": r_status,
                        "prior_attempts": attempts,
                        "preserved_candidates": cand_counts.get(fid, 0),
                        "has_verified_recording": fid in verified_ids,
                        "failed_providers": sorted(set(failed_transient_fams)),
                        "next_retry_utc": next_retry_for_track,
                        "reason": rec_reason,
                        "_audit": parsed_audit,
                    }
                )
                continue

            # Not demonstrably transient-failed -> check historical/config ambiguities
            if hard_blocked_fams or r_status == "unresolved":
                amb_reason = "ambiguous_hard_blocked_provider_audit"
                counts["ambiguous_total"] += 1
                ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                ambiguous_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "research_status": r_status,
                        "attempts": attempts,
                        "blocked_providers": sorted(set(hard_blocked_fams)),
                        "reason": amb_reason,
                    }
                )
                continue

            has_v202_meta = isinstance(parsed_audit.get("_meta"), dict)
            id_conf = str(row["identity_confidence"] or "")
            if (
                not has_v202_meta
                and id_conf == "filename_ambiguous"
                and (r_status == "no_match" or (r_status == "partial" and fid not in verified_ids))
            ):
                amb_reason = "ambiguous_v201_unrecorded_alt_orientation"
                counts["ambiguous_total"] += 1
                ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                ambiguous_items.append(
                    {
                        "file_id": fid,
                        "name": row["name"] or "",
                        "research_status": r_status,
                        "attempts": attempts,
                        "reason": amb_reason,
                    }
                )
                continue

            newly_cfg = cfg_families - recorded_clean_fams
            if newly_cfg and (newly_cfg & not_configured_fams or not has_v202_meta):
                # Only report if at least one currently configured catalogue provider was not_configured/absent then
                missing_then = [f for f in sorted(cfg_families) if f not in recorded_clean_fams]
                if missing_then:
                    amb_reason = "ambiguous_newly_configured_provider"
                    counts["ambiguous_total"] += 1
                    ambiguous_reasons[amb_reason] = ambiguous_reasons.get(amb_reason, 0) + 1
                    ambiguous_items.append(
                        {
                            "file_id": fid,
                            "name": row["name"] or "",
                            "research_status": r_status,
                            "attempts": attempts,
                            "unqueried_configured_providers": missing_then,
                            "reason": amb_reason,
                        }
                    )
                    continue

            counts["clean_completed_no_action"] += 1

        used_backup_str = ""
        if apply:
            if create_backup:
                created_bak = self.create_verified_backup(label="pre_v202_recovery")
                used_backup_str = str(created_bak)
            elif verified_backup_path is not None:
                bpath = Path(verified_backup_path).expanduser().resolve()
                if not bpath.exists() or bpath.stat().st_size == 0:
                    raise DatabaseMigrationError(f"Verified backup file does not exist or is empty: {bpath}")
                if bpath == self.db_path.resolve():
                    raise DatabaseMigrationError("Backup path must be a separate SQLite backup file, not the active database.")
                with sqlite3.connect(str(bpath), timeout=30.0) as bconn:
                    ok, msg = self._check_connection_integrity(bconn)
                    if not ok:
                        raise DatabaseMigrationError(f"Backup failed PRAGMA integrity_check ({msg}): {bpath}")
                    tbl_row = bconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'"
                    ).fetchone()
                    if not tbl_row:
                        raise DatabaseMigrationError(f"Backup file lacks 'tracks' table: {bpath}")
                used_backup_str = str(bpath)
            else:
                raise DatabaseMigrationError(
                    "Refusing to apply research recovery without a verified backup. "
                    "Pass --create-backup or --backup-file <VERIFIED_SQLITE_BACKUP>."
                )

            if recoverable_items:
                conn = self._get_connection(isolation_level=None)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    for item in recoverable_items:
                        fid = item["file_id"]
                        has_cands = item["preserved_candidates"] > 0
                        has_ver = item["has_verified_recording"]
                        new_status = (
                            "success" if has_ver else ("partial" if has_cands else "retryable_failure")
                        )
                        audit_copy = dict(item["_audit"])
                        meta_node = dict(audit_copy.get("_meta")) if isinstance(audit_copy.get("_meta"), dict) else {}
                        meta_node["recovered_in_v202_utc"] = now_iso
                        meta_node["prior_status_before_recovery"] = item["prior_status"]
                        meta_node["prior_attempts_before_recovery"] = item["prior_attempts"]
                        audit_copy["_meta"] = meta_node

                        conn.execute(
                            """
                            UPDATE tracks SET
                                research_status = ?,
                                research_incomplete = 1,
                                research_attempts = 0,
                                research_next_retry_utc = ?,
                                research_error = ?,
                                provider_calls_json = ?
                            WHERE file_id = ?
                              AND is_live_source = 1
                              AND quarantined = 0
                              AND research_status != 'permanent_unsupported'
                            """,
                            (
                                new_status,
                                item["next_retry_utc"],
                                f"Selective recovery ({item['reason']}); candidates preserved",
                                json.dumps(audit_copy, sort_keys=True),
                                fid,
                            ),
                        )
                        counts["applied_count"] += 1

                    conn.execute(
                        "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                        (
                            "RESEARCH_SELECTIVE_RECOVERY",
                            None,
                            json.dumps(
                                {
                                    "applied_count": counts["applied_count"],
                                    "reasons": recoverable_reasons,
                                    "backup_verified": used_backup_str,
                                }
                            ),
                            now_iso,
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()

        # Strip internal _audit dict before returning public report
        clean_recoverable = [{k: v for k, v in it.items() if k != "_audit"} for it in recoverable_items]
        return {
            "mode": "applied" if apply else "dry_run_preview",
            "verified_backup": used_backup_str,
            "counts": counts,
            "recoverable_reasons": recoverable_reasons,
            "ambiguous_reasons": ambiguous_reasons,
            "active_provider_cooldowns": active_cooldowns,
            "recoverable_tracks": clean_recoverable,
            "ambiguous_tracks": ambiguous_items,
        }

    # --- Local Read-Only Diagnostic Summary (Zero Network Calls) ---

    def get_inventory_summary(self) -> Dict[str, Any]:
        """Compute local database diagnostic counts with zero network calls."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            live = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0"
            ).fetchone()[0]
            quarantined = conn.execute("SELECT COUNT(*) FROM tracks WHERE quarantined = 1").fetchone()[0]
            checkpoint_only = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE record_origin = 'checkpoint_import' AND quarantined = 1"
            ).fetchone()[0]
            stale = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0 AND content_stale = 1"
            ).fetchone()[0]

            live_complete = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0
                  AND TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                """
            ).fetchone()[0]
            live_incomplete = live - live_complete

            hyd_counts = {
                r["hydration_status"]: int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT hydration_status, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY hydration_status
                    """
                ).fetchall()
            }
            res_counts = {
                r["research_status"]: int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT research_status, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY research_status
                    """
                ).fetchall()
            }
            conf_counts = {
                (r["identity_confidence"] or "unresolved"): int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT identity_confidence, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY identity_confidence
                    """
                ).fetchall()
            }
            total_bytes = conn.execute(
                "SELECT COALESCE(SUM(hydration_bytes), 0) FROM tracks"
            ).fetchone()[0]
            eligible_hydration = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0
                  AND hydration_status != 'permanent_unsupported'
                  AND (
                      content_stale = 1
                      OR hydration_status = 'pending'
                      OR (hydration_status = 'retryable_failure' AND hydration_attempts < 4)
                      OR (
                          hydration_status IN ('partial', 'unresolved')
                          AND (
                              (version != '' AND hydration_version != version)
                              OR NOT (
                                  hydration_method LIKE 'embedded_ranged%'
                                  OR hydration_method LIKE 'embedded_full%'
                                  OR hydration_method LIKE 'full_download%'
                                  OR hydration_method = 'preserved_same_version'
                              )
                          )
                      )
                  )
                """
            ).fetchone()[0]
            eligible_research = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0 AND content_stale = 0
                  AND TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                  AND (
                      research_status = 'pending'
                      OR (research_status = 'retryable_failure' AND research_attempts < 3)
                      OR (COALESCE(research_incomplete, 0) = 1 AND research_status NOT IN ('unresolved', 'permanent_unsupported') AND research_attempts < 3)
                  )
                """
            ).fetchone()[0]
            incomplete_research_cnt = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0 AND content_stale = 0
                  AND COALESCE(research_incomplete, 0) = 1
                """
            ).fetchone()[0]
            conflicts_cnt = conn.execute(
                "SELECT COUNT(*) FROM metadata_conflicts WHERE resolved = 0"
            ).fetchone()[0]
            stale_cands_cnt = conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE COALESCE(is_stale, 0) = 1"
            ).fetchone()[0]
            last_scan_row = conn.execute(
                "SELECT * FROM scan_runs ORDER BY scan_id DESC LIMIT 1"
            ).fetchone()
            mig_row = conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()

            return {
                "schema_version": int(mig_row["version"]) if mig_row else 1,
                "last_migration_backup": (mig_row["backup_path"] or "") if mig_row else "",
                "total_records": int(total),
                "live_source_tracks": int(live),
                "quarantined_records": int(quarantined),
                "checkpoint_only_records": int(checkpoint_only),
                "stale_live_tracks": int(stale),
                "live_ready_identity": int(live_complete),
                "live_missing_identity": int(live_incomplete),
                "eligible_for_hydration": int(eligible_hydration),
                "eligible_for_research": int(eligible_research),
                "incomplete_research_tracks": int(incomplete_research_cnt),
                "active_provider_cooldowns": self.get_all_provider_operational_states(active_only=True),
                "hydration_status_counts": hyd_counts,
                "research_status_counts": res_counts,
                "identity_confidence_counts": conf_counts,
                "total_hydration_bytes": int(total_bytes or 0),
                "unresolved_conflicts": int(conflicts_cnt),
                "archived_stale_candidates": int(stale_cands_cnt),
                "last_scan": dict(last_scan_row) if last_scan_row else None,
            }

    # --- Rollback Manifests ---

    def save_rollback_manifest(self, manifest_id: str, status: str, data: Dict[str, Any]) -> None:
        now = _utc_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rollback_manifests (manifest_id, status, created_utc, manifest_json)
                VALUES (?, ?, ?, ?)
                """,
                (manifest_id, status, now, json.dumps(data)),
            )

    def get_rollback_manifest(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM rollback_manifests WHERE manifest_id = ?", (manifest_id,)
            ).fetchone()
            return json.loads(row["manifest_json"]) if row else None

    def list_rollback_manifests(self) -> List[Tuple[str, str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT manifest_id, status, created_utc FROM rollback_manifests ORDER BY created_utc DESC"
            ).fetchall()
            return [(r["manifest_id"], r["status"], r["created_utc"]) for r in rows]
