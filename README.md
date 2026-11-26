# Music Vault Manager (`v2.0.3` — Full-Library Inventory & Resumable Read-Only Processing)

## MusicVault 2.0.3

MusicVault inventories the configured source and destination vault by exact Drive file ID, with folder-level resume checkpoints and atomic per-root publication. Unified processing (`musicvault process`) resumes bounded hydration, field-aware research with SonoVault quota protection, reconciliation, and local coverage/Sheets/routing previews. Destination files are eligible for research without becoming move-eligible. All Google Drive and Google Sheets mutations remain unconditionally blocked at the service request boundary.

- Use `musicvault process --scope all --inventory-only --dry-run` to preview inventory.
- Use `musicvault process --scope all` for unified processing and add `--resume` to continue a paused or interrupted run.
- On an existing database, verify shutdown and run `musicvault migrate --confirm-stopped` before processing.

[Debian PRoot upgrade, provider settings, and rollback](docs/V203_TERMUX.md) · [Changelog](CHANGELOG.md)


A modular Python CLI and terminal application for **Android (Termux & Debian/Ubuntu PRoot)** and Linux to inventory, hydrate audio tags, research recording metadata, and preview parent-genre organisation for DJ libraries stored on **Google Drive** and indexed in **Google Sheets**.

> **Safety Notice (`v2.0.3` Read-Only Build):**
> `v2.0.3` enforces **unconditional read-only protection against Google Drive and Google Sheets** (`GET`/`HEAD` only at the Google API request builder boundary). It never moves Drive files, creates Drive folders, renames files, modifies/transcodes audio, edits embedded tags, or writes to Google Sheets. Previews force `DRY_RUN=True`, `move_authorized=False`, and `executable=False` unconditionally regardless of `.env` flags.

---

## What Changed in `v2.0.2`

1. **Separated Provider Cooldowns, Hard Auth/Quota Blocks & Request Caps (`musicvault/providers/`)**:
   - Separates temporary server cooldowns (`COOLDOWN_DEFERRED` / `RATE_LIMITED` / `SERVER_ERROR`), hard credential/quota/endpoint blocks (`AUTH_ERROR`, `QUOTA_EXCEEDED`, `UNSUPPORTED_ENDPOINT`), and per-run `max_requests` caps (`BUDGET_EXHAUSTED`).
   - Parses RFC 9110 `Retry-After` (delta-seconds + HTTP-date), MusicBrainz `X-RateLimit-Reset` (Unix epoch seconds), IETF `RateLimit-Reset` (delta-seconds with epoch safety guard), and Discogs `0`-remaining windows **without shortening server waits** (including waits > `3600s`).
   - If a server-required wait exceeds the per-run retry sleep budget (`15s`), defers that provider immediately without sleeping or sending extra requests, continues querying other eligible providers, and persists minimal operational cooldown state (`provider_operational_state`, zero API payloads) across CLI restarts.
   - Enforces MusicBrainz `>= 1.1s` minimum pacing unconditionally and avoids naive substring quota false positives (`"plan"` in `"explanation"`).
2. **Honest Research Checkpoints & Non-Destructive Candidate Merging (`musicvault/core/researcher.py`, `musicvault/db.py`)**:
   - Records per-provider and per-orientation (`primary_query` + `alternate_query` for ambiguous `'A - B'` filenames) outcomes, HTTP call counts, cache hits, skipped calls, retry deadlines, and lookup completion.
   - Never treats deferred, blocked, or unattempted providers as definitive `no_match`, retains incomplete work (`research_incomplete=1`) when any provider remains retryable, never consumes `research_attempts` on pure cooldown/budget deferrals, and distinguishes verified recording identity without genre from routing readiness.
   - Replaces destructive candidate `DELETE` with transactional non-destructive candidate merging keyed by `(content_version, source_family, recording_edition_key)`, preserving stronger prior verification (`ISRC supported` / `Duration supported`) and timestamps against empty, transient, or weaker inferred responses.
3. **Selective Dry-Run-First Research Recovery (`musicvault recover-research`)**:
   - Inspects stored `provider_calls_json` audits to identify live, non-quarantined, current-content tracks stranded in `no_match`, `partial`, `success`, or exhausted `retryable_failure` due to demonstrable transient/cooldown/budget provider failures while reporting ambiguous historical records separately without resetting them.
   - Defaults to read-only dry-run preview; `--apply` requires an explicit verified SQLite backup (`--create-backup` or `--backup-file`), preserves all candidates and hydration metadata, respects active provider cooldowns, and is strictly idempotent.

## What Changed in `v2.0.1`

1. **Installer Distro Detection (`install.sh`)**:
   - Inspects `/etc/os-release` and effective UID first rather than assuming `command -v pkg` means native Termux (fixing `Cannot run pkg command as root` inside Debian Trixie PRoot where Termux `pkg` is inherited in `$PATH`).
   - Installs `python3-setuptools`, `python3-wheel`, and required packages via `apt-get` on Debian/Ubuntu, preserves existing `~/.config/musicvault/.env`, and verifies required Python imports before succeeding.
2. **Database Migration, Online Backups & Inventory Integrity (`musicvault/db.py`)**:
   - Automatically creates a **verified timestamped SQLite online backup** (`sqlite3.Connection.backup()` + `PRAGMA integrity_check`) before migrating `v2.0.0` databases to Schema `v2`—never overwriting `musicvault.db.bak` or prior backups, and never requiring credential or DB resets.
   - Separates confirmed live source inventory (`is_live_source=1`) from legacy benchmark/checkpoint imports (`quarantined=1`), reconciles by exact Google Drive `file_id`, repairs unchanged incomplete rows without needing Drive edits, archives stale evidence on content checksum changes, and never marks unseen files absent if a scan is interrupted.
3. **Bandwidth-Aware Ranged Audio Hydration (`musicvault hydrate`)**:
   - Uses bounded HTTP `Range` (`206 Partial Content`) reads via `DriveRangeReader` to seek directly to container headers and trailing chunks (skipping multi-megabyte PCM audio payloads and aborting immediately if a server returns `HTTP 200`).
   - Extracts both raw and easy Mutagen tags across **WAV (`ID3` + RIFF `LIST/INFO`), AIFF (`ID3`), FLAC/Vorbis, MP4/M4A, Ogg/Opus, and MP3**, preserving raw strings alongside normalized Camelot keys (`A minor` -> `8A`) and BPMs.
   - Safe filename parser strips track numbers without misassigning remixers (`01 Beneath The Lights - Darren Styles Remix.wav`) or title-only tracks (`01 Clarity.wav`) as verified primary artists.
4. **Resumable Research & Honest Provider Verification (`musicvault research`)**:
   - `--limit N` caps **actual attempted tracks** with known `artist + title`, advancing across batches rather than restarting alphabetically on missing-identity rows.
   - Setup wizard and providers inspect real HTTP status and response schemas (`401/403` auth errors, `429` `Retry-After` integer/HTTP-date, quota, `404`, malformed JSON, timeouts) and enforce UTC cache TTLs and `PROVIDER_TERMS_CLEARED=False` persistence rules.
5. **Real Read-Only Sheets/Drive Preview & Honest Benchmarks (`musicvault preview`, `musicvault benchmark`)**:
   - Menu option `5` and `musicvault preview` join live source tracks by exact Drive `file_id` against verified `Tracks!A1:AA` headers and rows (read-only), enforcing parent-genre-only routing, failing closed on malformed lock JSON or duplicate IDs, and never fabricating `row 999`.
   - `benchmark` runs inside an isolated temporary database without mutating `musicvault.db`, preserving the exact 10-track `v1.3` control regression (`8 eligible, 2 held, 0 authorized`) and reporting `INCOMPLETE_BENCHMARK` when expected rows are absent on 50-track checkpoints.

---

## Installation & Upgrade

**Requirements:** Python **3.11+** (verified on Python `3.11`, `3.12`, and `3.13` across Ubuntu 24.04 CI and Debian Trixie ARM64 PRoot).

### Fresh Clone (Target `feature/termux-modular-app` Until Merged)

Note: `main` currently holds only the initial placeholder README. Clone the active feature branch:

```bash
git clone -b feature/termux-modular-app https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-.git
cd Music-Organiser-Google-Drive-
chmod +x install.sh
```

- **Debian / Ubuntu PRoot on Termux (isolated `.venv` using apt prebuilt packages)**:
  ```bash
  ./install.sh --venv
  source .venv/bin/activate
  musicvault status
  ```
- **Debian / Ubuntu PRoot (system editable install matching `v2.0.0` layout)**:
  ```bash
  ./install.sh --system-editable
  # Runs apt-get (never pkg) + python3 -m pip install --no-deps --no-build-isolation --break-system-packages -e .
  musicvault status
  ```
- **Native Termux (non-root Android shell, outside PRoot)**:
  ```bash
  ./install.sh
  .venv/bin/musicvault status
  ```

### Upgrading an Existing `v2.0.0` Debian PRoot Checkout (Preserving Credentials & DB)

Do **not** delete `~/.config/musicvault/` or re-run `git clone`. From your existing repository directory:

```bash
cd ~/Music-Organiser-Google-Drive-
git status -u
git fetch origin
git switch fix/v2.0.1   # or feature/termux-modular-app once PR is merged
python3 -m pip install --no-deps --no-build-isolation --break-system-packages -e .
musicvault --version
musicvault status
```

---

## CLI Reference

```bash
# Local read-only diagnostic summary (zero network calls)
musicvault status [--verbose] [--export-json status.json] [--export-csv status.csv]

# Create an explicit verified timestamped SQLite online backup
musicvault backup [--label manual]

# Lightweight metadata-only Google Drive source inventory scan (0 audio bytes downloaded)
musicvault scan
# Optional scan followed by bounded hydration:
musicvault scan --hydrate --limit 10 --max-mib 32

# Bandwidth-bounded ranged audio tag & duration hydration (conservative defaults: 10 tracks, 32 MiB, no full downloads)
musicvault hydrate --limit 10 --max-mib 32 [--use-sheet] [--allow-full-download] [--export-csv hydrated.csv]

# Resumable online metadata research (--limit caps actual attempted tracks)
musicvault research --limit 10 [--force-refresh] [--include-title-only] [--no-wikidata] [--export-json research.json] [--export-csv research.csv]

# Selective dry-run-first research recovery (requires --apply + verified backup to mutate)
musicvault recover-research [--export-json recovery_preview.json] [--export-csv recovery_preview.csv]
musicvault recover-research --apply --create-backup   # or --backup-file /path/to/verified_backup.sqlite

# Real read-only Google Sheets + Drive parent-genre organisation preview
musicvault preview --limit 25 [--wide] [--verbose] [--export-csv preview.csv] [--export-json preview.json]

# Isolated offline fixture benchmarks (never imports into or mutates musicvault.db)
musicvault benchmark --size 10
musicvault benchmark --size 50 [--checkpoint /path/to/v06_local_checkpoint.json]

# Credential & Google OAuth setup wizard (reuses existing valid token.json)
musicvault setup
```

### Interactive Menu (`musicvault`)

Options `1–6` retain their `v2.0.0` positions (`5` now runs real live read-only preview instead of the 10-track mock benchmark); unsafe write toggles (`7/8` in `v2.0.0`) are replaced by bounded read-only tools:

- `1`: Run 10-Track Control Benchmark (Isolated Fixture)
- `2`: Run 50-Track Benchmark Check (Isolated Fixture)
- `3`: Scan Google Drive Source Inventory (Read-Only)
- `4`: Run Resumable Online Metadata Research (Limit 10)
- `5`: View Live Sheets & Drive Organisation Preview (Read-Only)
- `6`: Setup Credentials & Google Authentication Wizard
- `7` (`h`): Hydrate Audio Metadata Tags (Bounded 10 / 32 MiB)
- `8` (`s`): View Local Status & Diagnostics (No Network)
- `9` (`b`): Create Verified SQLite Database Backup
- `0` (`q`): Exit

---

## Android Background Execution, `tmux`, Wake-Locks & Troubleshooting

- **`tmux` vs Android OS Process Limits**:
  Running inside `tmux new -s vault` keeps `musicvault` alive if you switch apps or detach your terminal, **but Android 12+ Phantom Process Killer / battery saver can still terminate background Termux/PRoot processes**. Every `hydrate` and `research` track commits its SQLite checkpoint immediately, and `Ctrl+C` exits cleanly so interrupted batches resume without repeating completed work.
- **Wake-Lock Acquisition (Native Termux vs Debian PRoot)**:
  `musicvault status` reports wake-lock binary **detection** separately from **successful acquisition**. Inside Debian PRoot, calling `/data/data/com.termux/files/usr/bin/termux-wake-lock` may fail if Termux IPC environment variables are absent. For long runs, acquire the wake-lock in **native Termux before entering PRoot**:
  ```bash
  # In native Termux (requires Termux:API app + package installed):
  termux-wake-lock
  proot-distro login debian
  # ... run bounded musicvault batch ...
  # Later in native Termux:
  termux-wake-unlock
  ```
- **Google OAuth Token Reuse & 7-Day Testing Expiry**:
  Existing `~/.config/musicvault/token.json` and `client_secrets.json` are reused automatically with unchanged scopes. If your Google Cloud OAuth consent screen is configured as **External — Testing**, Google expires refresh tokens after **7 days**; only run `musicvault setup` to re-authenticate when prompted by an expired-refresh-token message.
- **Tagless or Ambiguous Audio Files**:
  `v2.0.1` never invents metadata. Truly tagless files with title-only names (e.g. `01 Clarity.wav`) or remix-only suffixes (`01 Beneath The Lights - Darren Styles Remix.wav`) remain honestly marked `unresolved` (`title_only`) rather than fabricating an artist.
- **Rollback Documentation**:
  See [docs/MIGRATION_AND_ROLLBACK.md](docs/MIGRATION_AND_ROLLBACK.md) for pre-migration SQLite verification and forensic rollback steps.

---

## User Device Acceptance Runbook (`v2.0.1`)

1. **Pre-Upgrade Verification & Manual SQLite Backup (in Debian PRoot with Music Vault closed)**:
   ```bash
   cd ~/Music-Organiser-Google-Drive-
   git status -u
   git branch --show-current

   python3 -c '
   import sqlite3
   from datetime import datetime, timezone
   from pathlib import Path
   db = Path.home() / ".config/musicvault/musicvault.db"
   ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
   bak = db.parent / f"musicvault.db.accept_pre_{ts}.sqlite"
   assert not bak.exists()
   with sqlite3.connect(str(db)) as src, sqlite3.connect(str(bak)) as dst:
       assert src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
       src.backup(dst)
       assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
   bak.chmod(0o600)
   print("Verified backup:", bak.name, "| historical rows:", sqlite3.connect(str(bak)).execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
   '
   ```
2. **Switch to Candidate Branch, Editable Install & Local Migration/Status Check**:
   ```bash
   git fetch origin
   git switch fix/v2.0.1
   python3 -m pip install --no-deps --no-build-isolation --break-system-packages -e .
   musicvault --help
   musicvault --version
   pytest -p no:typeguard -v tests/
   musicvault status
   ```
   Confirm `DRY_RUN=True (enforced)`, `Writes=DISABLED`, `DB Integrity: OK (schema v2)`, automatic `pre_v201_migration` backup file created, and all historical records preserved (`total_records` unchanged).
3. **Bounded Read-Only Scan, 10-Track Hydration (`32 MiB` cap), and Two Research Batches**:
   ```bash
   # Reconcile live source inventory vs checkpoint-only records (0 audio bytes downloaded)
   musicvault scan
   musicvault status

   # Hydrate at most 10 pending live tracks within 32 MiB ranged budget (no full downloads)
   musicvault hydrate --limit 10 --max-mib 32 --export-json hydrate_batch1.json

   # Research batch 1 (at most 10 eligible live tracks with artist+title)
   musicvault research --limit 10 --export-json research_batch1.json

   # Research batch 2 (verify queue advances without repeating completed tracks from batch 1)
   musicvault research --limit 10 --export-json research_batch2.json
   ```
4. **Bounded Read-Only Live Preview & Benchmark Verification**:
   ```bash
   musicvault preview --limit 25 --export-csv live_preview_sample.csv --export-json live_preview_sample.json
   musicvault benchmark --size 10
   musicvault benchmark --size 50
   ```
   Verify real `Tracks` sheet row numbers (never `999`), parent-only destinations, `Move-Authorized: 0`, `Executable: 0`, and zero Drive/Sheets modifications. If any critical discrepancy is observed, stop, keep dry-run active, and follow the forensic restore procedure in [docs/MIGRATION_AND_ROLLBACK.md](docs/MIGRATION_AND_ROLLBACK.md) without overwriting the active DB or deleting backups.
5. **Post-Acceptance**:
   Review exported summaries before running larger read-only hydration/research batches. Live Drive moves and Google Sheets writes remain out of scope for `v2.0.1` and require separate implementation review, rollback validation, and explicit user authorization in a future release.
