# Changelog

## 2.0.4

- Added full-vault spreadsheet intelligence honoring human verification locks and deliberate holds in `Tracks` columns `L` (`Processing Status`), `M` (`Analysis Confidence`), and `P` (`Notes / Override`, stripping automated `[Vault audit ...]` footers) alongside `Q:AA`.
- Treated `"Unclassified"`, `"Needs Review"`, and `"*Subgenre pending"` / `"*(review)"` cells as upgradable placeholders while strictly preserving existing text when evidence is `LOW`, `MISSING`, `CONFLICT`, or held under `TERMS_HOLD`.
- Added evidence-backed `"Dance / Club"` & `"Dance & Club"` $\to$ `"Hardstyle"` upgrades requiring recording-verified catalogue support without blanket club rewrites, artist-only inference, or BPM-only inference, while preserving genuine slow-tempo Hardstyle (`Slowtempo`).
- Re-enabled recording-verified granular subgenre classification (`Euphoric`, `Rawstyle`, `Rawphoric`, `Xtra Raw`, `Reverse Bass`, `Slowtempo`, `Uptempo`, `Frenchcore`, `Hard Techno`, `Liquid`, `Neurofunk`, `Jump Up`, `Dancefloor`, etc.) under the approved `SUBGENRE_PARENT` taxonomy.
- Normalized verified/proposed BPM display to rounded integers while retaining raw float measurements, provenance, same-integer clustering (`<= 0.5 BPM` spread), and track-corroborated `0.5x`/`2x` octave reconciliation.
- Strengthened edition matching across `original`, `remix`, `edit`, and `extended` versions.

## 2.0.3

- Added source and vault inventory by Drive file ID with folder-level resume checkpoints and atomic publication per root.
- Added explicit schema 3 migration (supporting both WAL and rollback-journal DBs), verified online backups, and per-database CLI locking.
- Added a resumable full-library processor with bounded hydration, essential-field SonoVault fallback protection (deferring on primary cooldowns/quotas without burning fallback credits), per-batch budget replenishment, bounded enrichment retries, persisted quota blocks (`clear-quota`), and circuit breakers with server deadline tracking.
- Restored v2.0.2-consistent `PROVIDER_TERMS_CLEARED=False` in-session research and candidate/coverage visibility while gating restricted `provider_cache` persistence and holding spreadsheet proposals under `TERMS_HOLD`.
- Added local coverage and Sheets/routing previews (decoupling parent-genre routing readiness from optional 10-field completeness, recognizing nested Vault genre subfolders, and holding overlapping roots across both unified and standalone previews).
- Blocked Drive and Sheets mutations unconditionally at the API request boundary.
