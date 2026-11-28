# MusicVault 2.0.3 on Debian PRoot

## Prepare the upgrade

Wait for the existing 17-batch loop to exit. Check both the shell that launched it and the Debian process list. Stop if research, hydration, inventory, or another MusicVault command is still running. Older versions do not use the v2.0.3 lock.

Run in Debian PRoot with the current installation still selected.

```bash
ps -eo pid,args | grep '[m]usicvault'
musicvault --version
musicvault status --export-json before-v203.json
musicvault backup --label pre_v203_reviewed_install
```

Keep the verified backup path printed by the last command. For a separate integrity check, open that exact file in read-only mode.

```bash
python3 -c '
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    print("Verified tracks:", db.execute("SELECT count(*) FROM tracks").fetchone()[0])
' /absolute/path/to/the/printed-backup.sqlite
```

## Install the reviewed commit

Substitute the reviewed commit SHA from the draft PR. Preserve your original checkout and use an adjacent directory for the candidate.

```bash
git clone --branch feature/v2.0.3 --single-branch \
  https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-.git musicvault-v203-review
cd musicvault-v203-review
git switch --detach REVIEWED_COMMIT_SHA
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install --no-deps --no-build-isolation -e .
musicvault --version
musicvault process --help
musicvault migrate --confirm-stopped
musicvault status --export-json after-v203.json
```

The editable install assumes the same Debian dependencies as v2.0.2, plus a PEP 660-capable setuptools and wheel. The migration creates a second verified online backup while excluding concurrent SQLite writers. Compare preserved counts and quarantine before proceeding. Use the same configured database directory as v2.0.2.

## Verify the roots and run a smoke batch

Check your configured `SOURCE_FOLDER_ID` and `VAULT_FOLDER_ID` locally. Counts 881 source, 1,387 vault, and 2,268 unique are historical. Account for changes, duplicates, overlaps, unsupported files, and inaccessible or skipped entries.

```bash
musicvault process --scope all --inventory-only --dry-run --export-json inventory-preview.json
musicvault process --scope all --inventory-only --export-json inventory-local.json
musicvault process --scope all --file-id NEW_ID_1 --file-id NEW_ID_2 --limit 2 \
  --export-json smoke-preview.json --export-csv smoke-preview.csv
musicvault process --scope all --batch-size 10 --export-json library-preview.json
musicvault process --scope all --resume --export-json library-preview.json
```

The dry run reads Drive and may read Sheets in full-process mode. It skips audio and provider calls and discards its local database snapshot. Inventory-only mode stops after inventory. Normal processing records checkpoints and enrichment in SQLite. Both modes enforce the cloud-write block. Use the same scope and original file selection when resuming. For a provider-specific resume, add `--provider PROVIDER_NAME`. Selective retries keep previously committed evidence.

A confirmed quota block with no documented reset stays held. After verifying the quota reset with the provider, run `musicvault clear-quota sonovault --confirm-reset` (which resets the provider block and re-opens tracks held solely by that quota error). Temporary throttles, timed quota blocks, and local breaker cooldowns are handled automatically. Use `PROVIDER_PRIORITY_JSON`, `REQUIRED_RESEARCH_PROVIDERS`, `FALLBACK_RESEARCH_PROVIDERS`, `DISABLED_RESEARCH_PROVIDERS`, and `SONOVAULT_MODE` to tune selection. In `SONOVAULT_MODE=fallback` (default), SonoVault is queried only when essential fields (`identity`, `genre`, `bpm`, or `key`) remain unresolved after eligible non-fallback providers complete; missing `subgenre`/`label` or primary-provider cooldowns/quota blocks defer without spending SonoVault credits. With `PROVIDER_TERMS_CLEARED=False` (default), configured providers run in-session with full local candidate and coverage visibility while restricted `provider_cache` payload retention stays off and spreadsheet proposals remain held under `TERMS_HOLD` until applicable terms are cleared.

## Stop and recover

Stop for unexpected record loss, unexplained quarantine growth, migration failure, or a write-policy regression. Rehearse recovery against a separate database copy and compare counts. Before any live recovery, stop all MusicVault processes and preserve the current database with an online backup, including committed WAL data. Obtain explicit approval before replacing newer progress. Run v2.0.2 only against a verified compatible restored copy. See [the earlier migration guide](MIGRATION_AND_ROLLBACK.md) for the existing forensic restore helper.
