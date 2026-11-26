#!/usr/bin/env bash
# ==============================================================================
# Music Vault Manager v2.0.2 — Automated Dependency & Installation Script
# Supports: Debian/Ubuntu (including Termux PRoot) and Native Termux on Android
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/setup.py" ]; then
    echo "[✗] Error: setup.py not found in $SCRIPT_DIR. Run install.sh from a Music Vault repository checkout." >&2
    exit 1
fi

OS_RELEASE_FILE="${MUSICVAULT_OS_RELEASE:-/etc/os-release}"
EFFECTIVE_UID="${MUSICVAULT_EFFECTIVE_UID:-$(id -u)}"
DRY_RUN_INSTALL="${MUSICVAULT_INSTALL_DRY_RUN:-0}"
DETECT_ONLY=0
SYSTEM_EDITABLE="${MUSICVAULT_SYSTEM_EDITABLE:-0}"
PYTHON_BIN="${MUSICVAULT_PYTHON_BIN:-python3}"

for arg in "$@"; do
    case "$arg" in
        --detect-only)
            DETECT_ONLY=1
            ;;
        --dry-run)
            DRY_RUN_INSTALL=1
            ;;
        --system-editable|--debian-system)
            SYSTEM_EDITABLE=1
            ;;
        --venv)
            SYSTEM_EDITABLE=0
            ;;
        --help|-h)
            cat <<EOF
Usage: ./install.sh [OPTIONS]

Options:
  --venv              Install into isolated .venv (default for fresh installs)
  --system-editable   Use Debian apt-backed editable install:
                      python3 -m pip install --no-deps --no-build-isolation --break-system-packages -e .
  --detect-only       Print detected environment/package manager and exit
  --dry-run           Print planned commands without modifying system packages
  -h, --help          Show this help message
EOF
            exit 0
            ;;
        *)
            echo "[✗] Unknown option: $arg (use --help)" >&2
            exit 2
            ;;
    esac
done

DISTRO_ID=""
DISTRO_LIKE=""
DISTRO_PRETTY=""
if [ -f "$OS_RELEASE_FILE" ]; then
    # Parse /etc/os-release safely without executing arbitrary code
    while IFS='=' read -r k v; do
        k="${k// /}"
        v="${v%\"}"
        v="${v#\"}"
        v="${v%\'}"
        v="${v#\'}"
        case "$k" in
            ID) DISTRO_ID="${v,,}" ;;
            ID_LIKE) DISTRO_LIKE="${v,,}" ;;
            PRETTY_NAME) DISTRO_PRETTY="$v" ;;
        esac
    done < "$OS_RELEASE_FILE"
fi

IS_DEBIAN_FAMILY=0
if [[ "$DISTRO_ID" == "debian" || "$DISTRO_ID" == "ubuntu" || "$DISTRO_LIKE" == *"debian"* || "$DISTRO_LIKE" == *"ubuntu"* ]]; then
    IS_DEBIAN_FAMILY=1
fi

HAS_APT=0
command -v apt-get >/dev/null 2>&1 && HAS_APT=1

HAS_PKG=0
command -v pkg >/dev/null 2>&1 && HAS_PKG=1

SELECTED_ENV=""
PKG_MGR=""

# Priority 1: Verified Linux distribution via /etc/os-release (e.g. Debian Trixie PRoot)
# Never select inherited Termux 'pkg' inside Debian/Ubuntu PRoot or as root.
if [ "$IS_DEBIAN_FAMILY" -eq 1 ]; then
    if [ "$HAS_APT" -eq 1 ]; then
        SELECTED_ENV="debian_apt"
        PKG_MGR="apt-get"
    else
        echo "[✗] Detected Debian/Ubuntu ($DISTRO_PRETTY) via $OS_RELEASE_FILE, but apt-get is unavailable." >&2
        exit 1
    fi
elif [ -n "$DISTRO_ID" ] && [ "$DISTRO_ID" != "android" ] && [ "$DISTRO_ID" != "termux" ]; then
    if [ "$HAS_APT" -eq 1 ]; then
        SELECTED_ENV="linux_apt"
        PKG_MGR="apt-get"
    else
        SELECTED_ENV="generic_linux"
        PKG_MGR="none"
    fi
else
    # No standard Linux distro in /etc/os-release -> check Native Termux
    if [ "$HAS_PKG" -eq 1 ]; then
        if [ "$EFFECTIVE_UID" = "0" ]; then
            echo "[✗] Refusing to run Termux 'pkg' as root (UID 0)." >&2
            echo "    If you are inside a PRoot distro, ensure /etc/os-release and apt-get are present." >&2
            echo "    If you are in native Termux, exit root/su before running ./install.sh." >&2
            exit 1
        fi
        SELECTED_ENV="native_termux"
        PKG_MGR="pkg"
    elif [ "$HAS_APT" -eq 1 ]; then
        SELECTED_ENV="linux_apt"
        PKG_MGR="apt-get"
    else
        SELECTED_ENV="generic_linux"
        PKG_MGR="none"
    fi
fi

if [ "$DETECT_ONLY" -eq 1 ]; then
    echo "SELECTED_ENV=$SELECTED_ENV"
    echo "PKG_MGR=$PKG_MGR"
    echo "DISTRO_ID=${DISTRO_ID:-none}"
    echo "EFFECTIVE_UID=$EFFECTIVE_UID"
    exit 0
fi

run_cmd() {
    if [ "$DRY_RUN_INSTALL" -eq 1 ]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

echo "=========================================================="
echo "    Music Vault Manager v2.0.2 — Environment Installer    "
echo "=========================================================="
echo "  Environment: $SELECTED_ENV (${DISTRO_PRETTY:-Termux/POSIX})"
echo "  Package mgr: $PKG_MGR"

# 1. Install OS / Distribution Dependencies
if [[ "$SELECTED_ENV" == "debian_apt" || "$SELECTED_ENV" == "linux_apt" ]]; then
    APT_PREFIX=()
    if [ "$EFFECTIVE_UID" != "0" ]; then
        if command -v sudo >/dev/null 2>&1; then
            APT_PREFIX=(sudo)
        else
            echo "[✗] Non-root user without sudo cannot run apt-get." >&2
            exit 1
        fi
    fi
    echo "[+] Installing Debian/Ubuntu packages via apt-get (never using Termux pkg)..."
    run_cmd "${APT_PREFIX[@]}" apt-get update -y
    run_cmd "${APT_PREFIX[@]}" apt-get install -y \
        python3 python3-pip python3-venv python3-setuptools python3-wheel \
        python3-mutagen python3-requests python3-cryptography \
        python3-googleapi python3-google-auth python3-google-auth-oauthlib \
        python3-google-auth-httplib2 python3-pytest tmux
elif [ "$SELECTED_ENV" = "native_termux" ]; then
    echo "[+] Installing native Termux packages via pkg..."
    run_cmd pkg update -y
    run_cmd pkg install -y python python-pip python-cryptography libjpeg-turbo ffmpeg termux-api tmux
else
    echo "[!] No managed OS package step for '$SELECTED_ENV'; relying on existing Python 3.11+ environment."
fi

# 2. Install Python Package (idempotent)
TARGET_PY="$PYTHON_BIN"
if [ "$SYSTEM_EDITABLE" -eq 1 ]; then
    echo "[+] Installing editable package into system environment (--system-editable)..."
    run_cmd "$PYTHON_BIN" -m pip install --no-deps --no-build-isolation --break-system-packages -e .
    TARGET_PY="$PYTHON_BIN"
else
    VENV_DIR="$SCRIPT_DIR/.venv"
    echo "[+] Preparing isolated virtual environment at $VENV_DIR..."
    if [[ "$SELECTED_ENV" == "debian_apt" || "$SELECTED_ENV" == "linux_apt" ]]; then
        run_cmd "$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR"
        if [ "$DRY_RUN_INSTALL" -eq 1 ]; then
            run_cmd "$VENV_DIR/bin/python" -m pip install --no-deps --no-build-isolation -e .
        else
            "$VENV_DIR/bin/python" -m pip install --no-deps --no-build-isolation -e .
            TARGET_PY="$VENV_DIR/bin/python"
        fi
    else
        run_cmd "$PYTHON_BIN" -m venv "$VENV_DIR"
        if [ "$DRY_RUN_INSTALL" -eq 1 ]; then
            run_cmd "$VENV_DIR/bin/python" -m pip install -e .
        else
            "$VENV_DIR/bin/python" -m pip install -e .
            TARGET_PY="$VENV_DIR/bin/python"
        fi
    fi
fi

# 3. Preserve Existing Configuration (never overwrite .env, tokens, or DB)
CONFIG_DIR="${MUSICVAULT_CONFIG_DIR:-$HOME/.config/musicvault}"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

if [ -f "$CONFIG_DIR/.env" ]; then
    echo "[✓] Preserved existing configuration at $CONFIG_DIR/.env"
elif [ -f "$SCRIPT_DIR/.env.example" ]; then
    cp "$SCRIPT_DIR/.env.example" "$CONFIG_DIR/.env"
    chmod 600 "$CONFIG_DIR/.env" 2>/dev/null || true
    echo "[+] Created initial template at $CONFIG_DIR/.env (permissions 0600)"
fi

# 4. Verify Required Imports Explicitly
echo "[+] Verifying required Python runtime imports..."
if [ "$DRY_RUN_INSTALL" -eq 0 ]; then
    if ! "$TARGET_PY" -c "
import sys
if sys.version_info < (3, 11):
    sys.stderr.write(f'[✗] Unsupported Python {sys.version.split()[0]}; Python 3.11+ is required.\n')
    sys.exit(1)
missing = []
for mod in ('mutagen', 'requests', 'googleapiclient', 'google.auth', 'google_auth_oauthlib', 'google_auth_httplib2', 'musicvault'):
    try:
        __import__(mod)
    except Exception as exc:
        missing.append(f'{mod} ({exc})')
if missing:
    sys.stderr.write('[✗] Missing required Python imports: ' + ', '.join(missing) + '\n')
    sys.exit(1)
import musicvault
print(f'[✓] Verified Python {sys.version.split()[0]} and musicvault v{musicvault.__version__}')
"; then
        echo "[✗] Installation check failed: required Python modules could not be imported." >&2
        exit 1
    fi
else
    echo "[DRY-RUN] Skipping live import check; verifying target interpreter ($TARGET_PY)..."
    "$TARGET_PY" -c "
import sys
if sys.version_info < (3, 11):
    sys.stderr.write(f'[✗] Unsupported Python {sys.version.split()[0]}; Python 3.11+ is required.\n')
    sys.exit(1)
for mod in ('mutagen', 'requests', 'googleapiclient', 'google.auth', 'google_auth_oauthlib', 'google_auth_httplib2'):
    __import__(mod)
"
fi

echo "=========================================================="
echo " [✓] Music Vault Manager v2.0.2 setup verified!"
if [ "$SYSTEM_EDITABLE" -eq 1 ]; then
    echo "     Run status check:   musicvault status"
    echo "     Launch menu:        musicvault"
else
    echo "     Activate venv:      source .venv/bin/activate"
    echo "     Run status check:   .venv/bin/musicvault status"
fi
echo "=========================================================="
