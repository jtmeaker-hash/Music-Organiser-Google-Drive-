# Changelog

## 2.0.3

- Added source and vault inventory by Drive file ID with folder-level resume checkpoints and atomic publication per root.
- Added explicit schema 3 migration (supporting both WAL and rollback-journal DBs), verified online backups, and per-database CLI locking.
- Added a resumable full-library processor with bounded hydration, essential-field SonoVault fallback protection (deferring on primary cooldowns/quotas without burning fallback credits), per-batch budget replenishment, bounded enrichment retries, persisted quota blocks (`clear-quota`), and circuit breakers with server deadline tracking.
- Restored v2.0.2-consistent `PROVIDER_TERMS_CLEARED=False` in-session research and candidate/coverage visibility while gating restricted `provider_cache` persistence and holding spreadsheet proposals under `TERMS_HOLD`.
- Added local coverage and Sheets/routing previews (decoupling parent-genre routing readiness from optional 10-field completeness, recognizing nested Vault genre subfolders, and holding overlapping roots across both unified and standalone previews).
- Blocked Drive and Sheets mutations unconditionally at the API request boundary.
