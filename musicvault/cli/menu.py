"""
Interactive Terminal Interface for Music Vault Manager.
"""
import sys
import os
from pathlib import Path
from typing import Optional

from ..config import Config, mask_secret
from ..db import Database
from ..termux import is_termux
from .wizard import run_setup_wizard
from .commands import run_benchmark, run_scan, run_research


def clear_screen() -> None:
    """Clear terminal screen."""
    os.system('clear' if os.name == 'posix' else 'cls')


def main_menu(config: Config, db: Database) -> None:
    """Main interactive terminal interface loop."""
    while True:
        tracks = db.get_all_tracks()
        termux_env = "Detected (Wake-lock ready)" if is_termux() else "Standard Linux"

        sono_status = "✓ Configured" if config.sonovault_api_key else "✗ Not Set"
        am_status = "✓ Configured" if config.audiometa_api_key else "✗ Not Set"
        dc_status = "✓ Configured" if config.discogs_token else "✗ Not Set"

        print("=" * 65)
        print("          MUSIC VAULT MANAGER — TERMINAL CONTROL PANEL          ")
        print("=" * 65)
        print(f"  Environment: {termux_env:<20} Database: {len(tracks)} tracks cached")
        print(f"  SonoVault:   {sono_status:<20} AudioMeta: {am_status}")
        print(f"  Discogs:     {dc_status:<20} Dry-Run:   {'Enabled' if config.dry_run else 'DISABLED'}")
        print("-" * 65)
        print("  1. Run 10-Track Benchmark Regression Test")
        print("  2. Run 50-Track Benchmark Test")
        print("  3. Scan Google Drive Inventory & Checkpoints")
        print("  4. Run Online Metadata Research & Genre Classification")
        print("  5. View Move & Organization Preview")
        print("  6. Setup Credentials & Google Authentication Wizard")
        print("  7. Toggle Dry-Run Mode (Currently: %s)" % ("ON" if config.dry_run else "OFF"))
        print("  8. Toggle Provisional Moves (Currently: %s)" % ("ALLOWED" if config.allow_provisional_moves else "HELD"))
        print("  0. Exit")
        print("=" * 65)

        choice = input("Select an option [0-8]: ").strip()

        if choice == '1':
            run_benchmark(config, db, sample_size=10)
            input("\nPress ENTER to return to menu...")
        elif choice == '2':
            run_benchmark(config, db, sample_size=50)
            input("\nPress ENTER to return to menu...")
        elif choice == '3':
            run_scan(config, db)
            input("\nPress ENTER to return to menu...")
        elif choice == '4':
            run_research(config, db, limit=10)
            input("\nPress ENTER to return to menu...")
        elif choice == '5':
            run_benchmark(config, db, sample_size=10)
            input("\nPress ENTER to return to menu...")
        elif choice == '6':
            run_setup_wizard(config)
            input("\nPress ENTER to return to menu...")
        elif choice == '7':
            new_state = not config.dry_run
            config.set("DRY_RUN", str(new_state))
            print(f"[✓] Dry-run mode set to {new_state}")
        elif choice == '8':
            new_prov = not config.allow_provisional_moves
            config.set("ALLOW_PROVISIONAL_MOVES", str(new_prov))
            print(f"[✓] Provisional moves set to {new_prov}")
        elif choice in ('0', 'q', 'exit'):
            print("\nExiting Music Vault Manager. Goodbye!\n")
            break
        else:
            print("[!] Invalid choice. Please enter a valid number.")
