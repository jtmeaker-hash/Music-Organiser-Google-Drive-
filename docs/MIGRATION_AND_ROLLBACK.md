# Database Migration, Inventory Reconciliation & Rollback Guide (v2.0.0 → v2.0.1)

## 1. Overview & Non-Destructive Guarantees

Music Vault Manager `v2.0.1` upgrades existing `v2.0.0` SQLite databases (`~/.config/musicvault/musicvault.db`) **in place, transactionally, and non-destructively**:

- **Zero credential resets**: `~/.config/musicvault/.env`, `client_secrets.json`, and `token.json` are never deleted, overwritten, or regenerated.
- **Verified pre-migration online backup**: Before applying any schema migration to an existing database, `Database` creates a **new, uniquely timestamped SQLite online backup** via Python's `sqlite3.Connection.backup()` API (capturing a consistent snapshot even if WAL/journal pages exist):
  - Format: `musicvault.db.pre_v201_migration_YYYYMMDDTHHMMSS_ffffffZ.sqlite` (restricted `0600` permissions).
  - Never overwrites `musicvault.db.bak` or any prior backup file.
  - Runs `PRAGMA integrity_check` on **both** the original database and the new backup, and verifies table row counts match before starting migration.
  - If either integrity check fails or any migration SQL statement raises an error, the transaction rolls back immediately and the original database remains untouched.
- **No `DROP TABLE` or rebuild from scratch**: All `v2.0.0` tables (`tracks`, `candidates`, `field_decisions`, `provider_cache`, `rollback_manifests`, `audit_log`) and rows are preserved.

---

## 2. Schema v2 Additions & Inventory Integrity Rules

### Why v2.0.0 Showed 908 Total Tracks vs 881 Live Source Tracks
In `v2.0.0`:
1. Running the 50-track benchmark imported `v06_local_checkpoint.json` (which contained 50 historical Colab tracks across both `SOURCE` and `VAULT`) directly into `tracks` with synthetic names (`"{artist} - {title}"`, e.g. `" - "` when empty) and blank `parent_id`.
2. Running `musicvault scan` skipped records whose `version` matched `md5Checksum` (leaving overlapping checkpoint rows with synthetic names and blank `parent_id`) and wrote `TrackMetadata(version=...)` with empty `artist`/`title`/`duration` for all other scanned tracks.

### How v2.0.1 Resolves This Safely
1. **Explicit Provenance & Quarantine (Zero Deletion)**:
   - Legacy checkpoint-only rows (blank `parent_id`, `size == 0`, no audio file extension) are preserved and marked `record_origin='checkpoint_import'`, `is_live_source=0`, `quarantined=1` so they never pollute live research or routing previews.
   - Blank or invalid `file_id` entries are quarantined (`quarantined=1`, `is_live_source=0`).
2. **Exact Drive File ID Reconciliation on Scan**:
   - Live inventory membership is recorded against `configured_source_id`, `observed_parent_id`, and `scan_generation`.
   - When `musicvault scan` sees an existing `file_id` (including one previously imported from a checkpoint), it refreshes `name`, `size`, `md5`, `modified_time`, and `observed_parent_id` **independently** of audio tags, clears `quarantined` if it is genuinely inside the configured source folder, and **preserves** existing non-blank `artist`/`title`/`duration`/`isrc` for the same content `version`.
   - Unchanged records that still lack `artist`/`title` are scheduled as `hydration_status='pending'` so `musicvault hydrate` repairs them without requiring a file modification on Drive.
   - If a file's content `version` (`md5Checksum` or `modifiedTime`) changes, prior metadata and candidates are archived into `evidence_history`, old candidates are marked `is_stale=1`, and the track is queued for fresh hydration and research.
   - **Interrupted scans never mark unseen files absent**: `is_live_source` membership is finalized only when a `scan_runs` pass completes cleanly without interruption or error.
3. **Isolated Benchmark Storage**:
   - `musicvault benchmark` runs in an isolated temporary database and never imports into or mutates `~/.config/musicvault/musicvault.db`.

---

## 3. Manual Pre-Upgrade Backup Command

Before switching branches or running migrations on a live device, create an explicit verified backup while Music Vault is stopped:

```bash
python3 -c '
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

db = Path.home() / ".config/musicvault/musicvault.db"
if db.exists():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = db.parent / f"musicvault.db.manual_{ts}.sqlite"
    assert not bak.exists(), f"Refusing to overwrite {bak}"
    with sqlite3.connect(str(db)) as src, sqlite3.connect(str(bak)) as dst:
        assert src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    bak.chmod(0o600)
    print("Verified backup created:", bak.name)
'
```

Once on `v2.0.1`, you can also run:

```bash
musicvault backup
```

---

## 4. Forensic Rollback Procedure

If any unexpected discrepancy occurs after upgrading:

1. **Stop Music Vault completely** (ensure no `musicvault` CLI or `tmux` job is accessing `musicvault.db`).
2. **Never overwrite the newer database directly and never delete backups**—preserve the post-upgrade database for forensics first:
   ```bash
   cd ~/.config/musicvault
   TS=$(date -u +%Y%m%dT%H%M%SZ)
   mv musicvault.db "musicvault.db.forensic_${TS}.sqlite"
   [ -f musicvault.db-wal ] && mv musicvault.db-wal "musicvault.db.forensic_${TS}.sqlite-wal"
   [ -f musicvault.db-shm ] && mv musicvault.db-shm "musicvault.db.forensic_${TS}.sqlite-shm"
   ```
3. **Verify your chosen backup before restoring a copy**:
   ```bash
   BACKUP_FILE="musicvault.db.manual_<TIMESTAMP>.sqlite"   # or musicvault.db.pre_v201_migration_<TIMESTAMP>.sqlite
   python3 -c "import sqlite3; c=sqlite3.connect('$BACKUP_FILE'); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; print('Backup OK')"
   cp -p "$BACKUP_FILE" musicvault.db
   chmod 600 musicvault.db
   ```
4. Switch git back to the prior commit/branch if reverting code:
   ```bash
   cd ~/Music-Organiser-Google-Drive-
   git switch feature/termux-modular-app
   ```

---

## 5. Residual Cloud Write Limitations (Disabled in v2.0.1)

`v2.0.1` is strictly a **read-only cloud corrective build**:
- `DriveService.move_file`, `DriveService.ensure_folder`, and `SheetsService.apply_batch_updates` are **not wired** into the live CLI or interactive menu.
- Live `preview` forces dry-run (`move_authorized=False`, `executable=False`) regardless of `.env` flags.
- Note for future releases: Google Sheets REST `values.get` followed by `values.batchUpdate` is inherently non-atomic (a narrow TOCTOU window exists between pre-write row re-read and `batchUpdate`), so future write enablement will require explicit per-batch human review, pre-write row backups, and strict fail-closed lock validation.
