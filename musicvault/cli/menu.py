"""
Responsive Interactive Terminal Interface for Music Vault Manager v2.0.1.
Preserves options 1-6 workflow slots, adds bounded Hydrate, Local Status, and Backup,
and enforces read-only cloud safety (no live Drive moves or Sheet writes in v2.0.1).
"""
from .. import __version__
from ..config import Config
from ..db import Database
from ..termux import detect_environment
from .commands import (
    run_backup,
    run_benchmark,
    run_hydrate,
    run_preview,
    run_research,
    run_scan,
    run_status,
)
from .formatting import get_terminal_width
from .wizard import run_setup_wizard


def main_menu(config: Config, db: Database) -> None:
    """Main interactive terminal interface loop with Ctrl+C safety."""
    while True:
        try:
            inv = db.get_inventory_summary()
            env_info = detect_environment()
            width = min(max(get_terminal_width(), 44), 68)

            sono_st = "✓ Set" if config.sonovault_api_key else "✗ Unset"
            am_st = "✓ Set" if config.audiometa_api_key else "✗ Unset"
            dc_st = "✓ Set" if config.discogs_token else "✗ Unset"

            print("=" * width)
            print(f"  MUSIC VAULT MANAGER v{__version__} (READ-ONLY CANDIDATE)")
            print("=" * width)
            print(f"  Env:       {env_info.short_label()}")
            print(
                f"  Inventory: {inv['live_source_tracks']} live | "
                f"{inv['live_ready_identity']} ready | "
                f"{inv['quarantined_records']} quarantined"
            )
            print(f"  Providers: SonoVault:{sono_st} | AudioMeta:{am_st} | Discogs:{dc_st}")
            print("  Safety:    DRY-RUN ENFORCED (Cloud writes disabled in v2.0.1)")
            print("-" * width)
            print("  1. Run 10-Track Control Benchmark (Isolated Fixture)")
            print("  2. Run 50-Track Benchmark Check (Isolated Fixture)")
            print("  3. Scan Google Drive Source Inventory (Read-Only)")
            print("  4. Run Resumable Online Metadata Research (Limit 10)")
            print("  5. View Live Sheets & Drive Organisation Preview (Read-Only)")
            print("  6. Setup Credentials & Google Authentication Wizard")
            print("  7. Hydrate Audio Metadata Tags (Bounded 10 / 32 MiB)")
            print("  8. View Local Status & Diagnostics (No Network)")
            print("  9. Create Verified SQLite Database Backup")
            print("  0. Exit")
            print("=" * width)

            choice = input("Select an option [0-9]: ").strip().lower()

            if choice == "1":
                run_benchmark(config, db, sample_size=10)
                input("\nPress ENTER to return to menu...")
            elif choice == "2":
                run_benchmark(config, db, sample_size=50)
                input("\nPress ENTER to return to menu...")
            elif choice == "3":
                run_scan(config, db)
                input("\nPress ENTER to return to menu...")
            elif choice == "4":
                run_research(config, db, limit=10)
                input("\nPress ENTER to return to menu...")
            elif choice == "5":
                run_preview(config, db, limit=25)
                input("\nPress ENTER to return to menu...")
            elif choice == "6":
                run_setup_wizard(config)
                input("\nPress ENTER to return to menu...")
            elif choice in ("7", "h", "hydrate"):
                run_hydrate(config, db, limit=10, max_mib=32.0, allow_full_download=False)
                input("\nPress ENTER to return to menu...")
            elif choice in ("8", "s", "status"):
                run_status(config, db)
                input("\nPress ENTER to return to menu...")
            elif choice in ("9", "b", "backup"):
                run_backup(config, db, label="menu_manual")
                input("\nPress ENTER to return to menu...")
            elif choice in ("0", "q", "exit"):
                print("\nExiting Music Vault Manager. Goodbye!\n")
                break
            else:
                print(
                    "[!] Invalid option. Enter 0-9 (note: live write/move toggles are unavailable in read-only v2.0.1)."
                )
        except (KeyboardInterrupt, EOFError):
            print("\n[i] Interrupted cleanly. Checkpoints preserved. Goodbye!\n")
            break
