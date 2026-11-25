# Music Vault Manager

A standalone, modular Python application designed to run natively in **Termux on Android** for managing, researching, verifying, and organizing large personal DJ music libraries stored on Google Drive and indexed via Google Sheets.

Originally conceived as a Google Colab notebook (`v1.3` and `v1.4 Beta`), this architecture has been completely redesigned into a robust, decoupled, and production-ready terminal tool.

---

## 🌟 Key Features

- **Native Termux & Android ARM64 Compatibility**:
  - Requires no desktop computer or Google Colab session.
  - Native integration with Android wake-locks (`termux-wake-lock`) to keep background batch processes running even with the screen off.
  - Zero heavy or non-portable binary compilation dependencies.
- **Interactive Terminal Interface**:
  - Full-featured, responsive TUI menu for scanning, researching, reviewing, and organizing.
  - Simple CLI entrypoint: `musicvault`.
- **Persistent SQLite Database**:
  - Replaces fragile JSON checkpoints with an ACID-compliant SQLite store (`~/.config/musicvault/musicvault.db`).
  - Resumable processing: automatic per-track checkpoints protect against battery failure, network disconnection, or process termination.
  - TTL-based API response caching respecting provider terms and rate limits.
- **Intelligent Parent Genre Classification**:
  - Prioritizes accurate **parent genre** routing (`Hardstyle`, `Drum & Bass`, `Techno`, `Hip-Hop`, `Trance`, `House`, etc.).
  - Preserves existing subgenre data while temporarily disabling uncertain subgenre branching.
  - Detects novel genres (with 2+ provider agreement) and proposes new directory creation rather than forcing tracks into rigid predefined buckets.
  - Cross-checks existing sheet classifications against fresh catalogue evidence, flagging conflicts (`CONFLICT`, `INFERENCE_CONFLICT_REVIEW`).
- **Resilient Online Metadata Research**:
  - Resolves previous notebook failures (such as Reddit `HTTP 403 Forbidden` and brittle HTML scrapers).
  - Integrates legitimate open music knowledge graphs: **MusicBrainz**, **Wikidata Music Ontology**, **Discogs**, **SonoVault**, and **AudioMeta**.
  - Rate-limited and respectful HTTP clients with exponential backoff and `Retry-After` adherence.
- **BPM & Camelot Key Identification**:
  - Identifies musical tempos and harmonic keys formatted in standard Camelot alphanumeric notation (e.g. `8A`, `11B`, `5A`).
  - Detects half-time / double-time tempo hints for Hard dance genres.
- **Safe Automated Library Organization**:
  - **Dry-run mode enabled by default**.
  - All Google Drive moves are performed purely via metadata parent updates (`addParents` / `removeParents`), preserving original file IDs, metadata tags, and audio content without downloading, re-encoding, or transcoding.
  - Pre-flight checks ensure files are not trashed, are not folders, and have exactly one parent before moving.
  - Generates JSON rollback manifests and database transaction logs prior to executing any move.
- **Master Spreadsheet Preservation**:
  - Updates only empty fields and dedicated review columns `Q:AA` in the master `Tracks` tab.
  - Never overwrites existing user values or fields flagged `HIGH_LOCKED` / `USER_LOCKED`.
  - Automatically flags uncertain classifications with review stars (`★`) in the same master sheet.
  - Pre-flight concurrent edit detection aborts writes if another client modified rows during review.
  - Local JSON row backups created before every write.
- **First-Run Credential Wizard**:
  - Replaces Colab `userdata` secrets with a secure local wizard.
  - Stores credentials in `~/.config/musicvault/.env` with strict `0600` (user read/write only) file permissions.
  - Masks credential entry in the terminal and never prints secrets in logs or reports.

---

## 📐 Architecture Overview

```
Music-Organiser-Google-Drive-/
├── musicvault/                        # Core application package
│   ├── __init__.py
│   ├── __main__.py                    # CLI entrypoint (musicvault)
│   ├── config.py                      # Secure .env loader (0600) & settings
│   ├── auth.py                        # Google OAuth2 manager & persistent tokens
│   ├── db.py                          # SQLite database, caching & checkpoints
│   ├── models.py                      # Data models (Track, Candidate, Decisions)
│   ├── termux.py                      # Android wake-lock integration
│   ├── core/                          # Domain logic
│   │   ├── matcher.py                 # Normalization, Camelot key & candidate verification
│   │   ├── audio.py                   # Mutagen audio tag extraction
│   │   ├── genre.py                   # Parent genre taxonomy & alias normalization
│   │   ├── rules.py                   # Field-level decision engine (HIGH/MEDIUM/LOW/LOCK)
│   │   └── organizer.py               # Drive routing, preview, rollback manifest
│   ├── providers/                     # Metadata API providers
│   │   ├── base.py                    # Rate-limiting, retry backoff & cache
│   │   ├── sonovault.py               # SonoVault API client
│   │   ├── audiometa.py               # AudioMeta API client
│   │   ├── discogs.py                 # Discogs API client
│   │   ├── musicbrainz.py             # MusicBrainz Web Service client
│   │   └── research.py                # Wikidata & knowledge graph research
│   ├── services/                      # Google Cloud API services
│   │   ├── drive.py                   # Google Drive v3 client
│   │   └── sheets.py                  # Google Sheets v4 client
│   └── cli/                           # Terminal UI
│       ├── menu.py                    # Interactive control panel
│       ├── wizard.py                  # Credential setup wizard
│       └── commands.py                # Command implementations
├── tests/                             # Comprehensive test suite (14 unit tests)
│   ├── test_decision_engine.py       # Replicates v1.3 self_test()
│   ├── test_routing_engine.py        # Replicates v1.3 routing_self_test()
│   ├── test_genre_classifier.py      # Replicates v1.4 parent routing self_test()
│   ├── test_matcher.py               # Camelot, aliases, and audio verification tests
│   ├── test_db_persistence.py        # SQLite persistence and cache tests
│   └── test_benchmark_10.py          # 10-track control benchmark test
├── reference/                         # Preserved original notebooks and test reports
│   ├── 01_Antigravity_Prompt.txt
│   ├── 02_Latest_Test_Report_v1.3.txt
│   ├── 03_API_Secrets_and_Authentication.txt
│   ├── Music_Vault_Manager_v1.3_Provisional_Routing_Beta.ipynb
│   └── Music_Vault_Manager_v1.4_Parent_Genre_Beta.ipynb
├── install.sh                         # Automated dependency installer for Android/Termux
├── setup.py                           # Package configuration
├── requirements.txt                   # Dependency manifest
└── .env.example                       # Configuration template
```

---

## 🚀 Installation on Termux / Android

### 1. Automated Installation
Run the automated installation script:
```bash
git clone https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-.git
cd Music-Organiser-Google-Drive-
chmod +x install.sh
./install.sh
```

### 2. Manual Installation
If installing packages manually on Debian/PRoot or native Termux:
```bash
# Debian / PRoot:
apt-get update
apt-get install -y python3 python3-pip python3-mutagen python3-requests \
    python3-cryptography python3-googleapi python3-google-auth \
    python3-google-auth-oauthlib python3-google-auth-httplib2 python3-pytest

# Native Termux:
pkg update
pkg install -y python python-pip python-cryptography termux-api

# Install musicvault package
pip3 install --no-deps --break-system-packages -e .
```

---

## 🔑 Configuration & First-Run Wizard

Run the interactive setup wizard:
```bash
musicvault setup
```
The wizard prompts for:
1. `SONOVAULT_API_KEY`: SonoVault API key for BPM, Key, and Genre.
2. `AUDIOMETA_API_KEY`: AudioMeta API key for acoustic features and Camelot key analysis.
3. `DISCOGS_TOKEN`: Discogs personal access token for release metadata and label profiles.
4. **Google Drive & Sheets OAuth**: Interactive authentication that saves the persistent OAuth token to `~/.config/musicvault/token.json` (`0600`).

All secrets are saved in `~/.config/musicvault/.env` with strict `0600` permissions. You can also edit this file directly or set standard environment variables.

---

## 💻 Usage

### 1. Interactive Terminal Menu
Simply run:
```bash
musicvault
```
This opens the control panel:
```
=================================================================
          MUSIC VAULT MANAGER — TERMINAL CONTROL PANEL          
=================================================================
  Environment: Detected (Wake-lock ready) Database: 50 tracks cached
  SonoVault:   ✓ Configured           AudioMeta: ✓ Configured
  Discogs:     ✓ Configured           Dry-Run:   Enabled
-----------------------------------------------------------------
  1. Run 10-Track Benchmark Regression Test
  2. Run 50-Track Benchmark Test
  3. Scan Google Drive Inventory & Checkpoints
  4. Run Online Metadata Research & Genre Classification
  5. View Move & Organization Preview
  6. Setup Credentials & Google Authentication Wizard
  7. Toggle Dry-Run Mode (Currently: ON)
  8. Toggle Provisional Moves (Currently: HELD)
  0. Exit
=================================================================
```

### 2. Command Line Interface

```bash
# Run 10-track control benchmark test
musicvault benchmark --size 10

# Run 50-track test with specific checkpoint
musicvault benchmark --size 50 --checkpoint /mnt/sdcard/Download/v06_local_checkpoint.json

# Scan Google Drive incoming source folder
musicvault scan

# Run metadata research for up to 20 tracks
musicvault research --limit 20
```

---

## 🧪 Benchmark Regression Testing

The 10-track benchmark reproduces the exact decision rules, star indicators, and provisional move routing established in `v1.3`:

```
=== MOVE PREVIEW — v1.3 ===
row    file_id                             track                               destination     reason                    routing_confidence   eligible   move_authorized
---------------------------------------------------------------------------------------------------------------------------------------------------------------------
2      1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX   Believe - Jesse Bloch, Coby Watts                   UNKNOWN_OR_UNSUPPORTED_GENRE HOLD                 False      False
2169   1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW   Atmozfears & David Spekter - Rele   Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
7      1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL   Act of Rage - Living Fast           Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
23     1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf   Big City Life - Luude, Mattafix     Drum & Bass     PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
159    1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB   Dexperia - Amen                     Drum & Bass     PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
355    1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK   Another High - Wasted Penguinz      Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
1914   1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18   Rebelion; Micah Martin - The Loud   Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
1040   1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-   Young, Wild & Free (feat. Bruno M                   UNKNOWN_OR_UNSUPPORTED_GENRE HOLD                 False      False
2181   1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks   Hard Driver - You're Not Alone      Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
1204   1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C   Adjuzt - You're Not Original        Hardstyle       PROVISIONAL_EXISTING_GENRE PROVISIONAL_REVIEW   True       False
Eligible: 8 of 10 | move-authorized: 0 | DRY_RUN: True
```

Run test suite via `pytest`:
```bash
pytest -v tests/
```
All 14 unit tests pass cleanly:
- Decision rules (`HIGH`, `MEDIUM`, `LOW`, `LOCK`)
- Preservation of existing spreadsheet cells
- Parent genre classification & alias normalization
- Camelot key calculation
- Drive move routing logic & provisional gates
- SQLite persistence & caching

---

## 🛡️ Safety & Rollback Guarantees

1. **Dry-Run by Default**: No spreadsheet changes and no Drive moves are executed without explicit confirmation.
2. **Audio Integrity**: Original audio files are never modified, re-encoded, tagged on disk, or deleted.
3. **Drive File ID Preservation**: Organizing files on Google Drive updates folder parents; file IDs never change.
4. **Pre-flight Checks**:
   - Confirms each file exists, is not trashed, and has exactly one parent.
   - Detects concurrent Google Sheets edits before applying cell updates.
5. **Rollback Manifest**:
   - Before executing moves, a rollback manifest (`music_vault_move_rollback.json` and SQLite record) is saved.
   - Any move operation can be reversed to return files to their original parent folders.

---

## 📱 Background Execution on Android

To run long-running batch scans or research without interruption:
1. **Tmux**:
   ```bash
   tmux new -s vault
   musicvault
   # Detach using Ctrl+B then D
   ```
2. **Wake-Lock**:
   `musicvault` automatically invokes `termux-wake-lock` during processing tasks to prevent Android from putting the CPU to sleep.

---

## 📄 License
MIT License. Created by Jacob Tyler Meaker.
