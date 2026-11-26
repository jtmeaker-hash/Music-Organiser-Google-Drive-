# Changelog

## 2.0.3

- Added source and vault inventory by Drive file ID with atomic publication per root.
- Added explicit schema 3 migration, verified online backups, and per-database CLI locking.
- Added a resumable full-library processor with bounded hydration, audited provider fallback, durable enrichment, persisted quota blocks, and circuit breakers.
- Added local coverage and Sheets/routing previews. Provider attribution uses stored, permitted evidence.
- Blocked Drive and Sheets mutations at the API request boundary.
