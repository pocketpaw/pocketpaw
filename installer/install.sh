#!/bin/sh
# PocketPaw Installer Bootstrap
# Usage: curl -fsSL https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/install.sh | sh
# POSIX sh — no bashisms

set -e

# ── Banner ──────────────────────────────────────────────────────────────
printf '\n'
printf '  \033[1;35m┌─────────────────────────────────────────┐\033[0m\n'
printf '  \033[1;35m│\033[0m  \033[1m🐾  PocketPaw Installer\033[0m                  \033[1;35m│\033[0m\n'
printf '  \033[1;35m│\033[0m  The AI agent that runs on your laptop   \033[1;35m│\033[0m\n'
printf '  \033[1;35m└─────────────────────────────────────────┘\033[0m\n'
printf '\n'

# ── OS Detection ────────────────────────────────────────────────────────
OS="$(uname -s 2>/dev/null || echo Unknown)"
case "$OS" in
    CYGWIN*|MINGW*|MSYS*|Windows_NT)
        printf '\033[33mNote:\033[0m On Windows, use the PowerShell installer instead:\n'
        printf '       irm https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/install.ps1 | iex\n'
        printf '\n'
        printf '       Or use WSL (Windows Subsystem for Linux):\n'
        printf '       https://learn.microsoft.com/windows/wsl/install\n'
        exit 1
        ;;
esac

# ── Download helper ─────────────────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
    DOWNLOAD="curl -fsSL -H Cache-Control:no-cache"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD="wget -qO- --no-cache"
else
    printf '\033[31mError:\033[0m Neither curl nor wget found.\n'
    exit 1
fi

# ── ensure_uv() — install uv if not present ────────────────────────────
UV_AVAILABLE=0

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV_AVAILABLE=1
        return 0
    fi

    printf '  Installing uv (fast Python package manager)...\n'
    if $DOWNLOAD https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
        # Refresh PATH to pick up the new binary
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command -v uv >/dev/null 2>&1; then
            UV_AVAILABLE=1
            printf '  \033[32m✓\033[0m uv installed\n'
            return 0
        fi
    fi

    printf '  \033[33mWarn:\033[0m Could not install uv automatically.\n'
    return 1
}

# ── Find Python 3.11+ ──────────────────────────────────────────────────
find_python() {
    for cmd in python3 python3.13 python3.12 python3.11 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                PYTHON="$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=""
if ! find_python; then
    # Cascade 1: install via uv
    printf '  Python 3.11+ not found. Attempting to install...\n'
    if ensure_uv; then
        printf '  Installing Python 3.12 via uv...\n'
        if uv python install 3.12 >/dev/null 2>&1; then
            uv_python_path=$(uv python find 3.12 2>/dev/null || echo "")
            if [ -n "$uv_python_path" ]; then
                PYTHON="$uv_python_path"
                printf '  \033[32m✓\033[0m Python 3.12 installed via uv\n'
            fi
        fi
    fi

    # Cascade 2: system package manager
    if [ -z "$PYTHON" ]; then
        case "$OS" in
            Darwin)
                if command -v brew >/dev/null 2>&1; then
                    printf '  Installing Python 3.12 via Homebrew...\n'
                    if brew install python@3.12 >/dev/null 2>&1; then
                        find_python
                    fi
                fi
                ;;
            Linux)
                if command -v apt-get >/dev/null 2>&1; then
                    printf '  Installing Python 3.12 via apt...\n'
                    if sudo apt-get update -qq >/dev/null 2>&1 && \
                       sudo apt-get install -y -qq python3.12 python3.12-venv >/dev/null 2>&1; then
                        find_python
                    fi
                elif command -v dnf >/dev/null 2>&1; then
                    printf '  Installing Python 3.12 via dnf...\n'
                    if sudo dnf install -y -q python3.12 >/dev/null 2>&1; then
                        find_python
                    fi
                elif command -v pacman >/dev/null 2>&1; then
                    printf '  Installing Python via pacman...\n'
                    if sudo pacman -S --noconfirm python >/dev/null 2>&1; then
                        find_python
                    fi
                fi
                ;;
        esac
    fi

    # Cascade 3: hard exit
    if [ -z "$PYTHON" ]; then
        printf '\033[31mError:\033[0m Python 3.11+ is required but could not be installed.\n'
        printf '       Install manually:\n'
        printf '         curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12\n'
        case "$OS" in
            Darwin) printf '         Or: brew install python@3.12\n' ;;
            Linux)  printf '         Or: sudo apt install python3.12 (Debian/Ubuntu)\n'
                    printf '             sudo dnf install python3.12 (Fedora)\n'
                    printf '             sudo pacman -S python (Arch)\n' ;;
        esac
        exit 1
    fi
fi

PYTHON_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
printf '  Python:  %s (%s)\n' "$PYTHON_VER" "$(command -v "$PYTHON" 2>/dev/null || echo "$PYTHON")"

# ── Find package installer (uv-first strategy) ─────────────────────────
# Always try to get uv first — it handles PEP 668 transparently (no sudo needed)
ensure_uv

PIP_CMD=""
if [ "$UV_AVAILABLE" = "1" ]; then
    PIP_CMD="uv pip"
    printf '  Installer: uv pip\n'
elif "$PYTHON" -m pip --version >/dev/null 2>&1; then
    PIP_CMD="$PYTHON -m pip"
    printf '  Installer: pip (%s)\n' "$("$PYTHON" -m pip --version 2>/dev/null | cut -d' ' -f2)"
elif command -v pip3 >/dev/null 2>&1; then
    PIP_CMD="pip3"
    printf '  Installer: pip3\n'
elif command -v pip >/dev/null 2>&1; then
    PIP_CMD="pip"
    printf '  Installer: pip\n'
fi

# Fallback: bootstrap pip if nothing found
if [ -z "$PIP_CMD" ]; then
    printf '  No package installer found. Bootstrapping pip...\n'

    # ensurepip
    if "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1; then
        PIP_CMD="$PYTHON -m pip"
        printf '  \033[32m✓\033[0m pip bootstrapped via ensurepip\n'
    fi

    # get-pip.py
    if [ -z "$PIP_CMD" ]; then
        if $DOWNLOAD https://bootstrap.pypa.io/get-pip.py | "$PYTHON" - --user >/dev/null 2>&1; then
            export PATH="$HOME/.local/bin:$PATH"
            PIP_CMD="$PYTHON -m pip"
            printf '  \033[32m✓\033[0m pip installed via get-pip.py\n'
        fi
    fi

    # Hard exit
    if [ -z "$PIP_CMD" ]; then
        printf '\033[31mError:\033[0m No pip or uv could be installed.\n'
        printf '       Install uv manually:\n'
        printf '         curl -LsSf https://astral.sh/uv/install.sh | sh\n'
        printf '       Then re-run this installer.\n'
        exit 1
    fi
fi

printf '\n'

# ── Download installer.py ──────────────────────────────────────────────
TMPDIR="${TMPDIR:-/tmp}"
INSTALLER="$TMPDIR/pocketpaw_installer.py"

# Clean up on exit
cleanup() { rm -f "$INSTALLER"; }
trap cleanup EXIT INT TERM

INSTALLER_URL="https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py"

if command -v curl >/dev/null 2>&1; then
    DOWNLOAD="curl -fsSL"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD="wget -qO-"
else
    printf '\033[31mError:\033[0m Neither curl nor wget found.\n'
    exit 1
fi

printf '  Downloading installer...\n'
if ! $DOWNLOAD "$INSTALLER_URL" > "$INSTALLER" 2>/dev/null; then
    printf '\033[33mWarn:\033[0m Primary download failed, trying fallback...\n'
    FALLBACK_URL="https://raw.githubusercontent.com/pocketpaw/pocketpaw/dev/installer/installer.py"
    if ! $DOWNLOAD "$FALLBACK_URL" > "$INSTALLER" 2>/dev/null; then
        printf '\033[31mError:\033[0m Could not download installer.\n'
        printf '       Try manually: %s\n' "$INSTALLER_URL"
        exit 1
    fi
fi

# Verify it looks like Python
if ! head -1 "$INSTALLER" | grep -q "^#\|^\"\"\"\|^import\|^from\|^def\|^class"; then
    printf '\033[31mError:\033[0m Downloaded file does not look like a Python script.\n'
    printf '       Check your network connection and try again.\n'
    exit 1
fi

printf '  Launching interactive installer...\n\n'

# ── Run installer ──────────────────────────────────────────────────────
EXTRA_FLAGS="--from-git"
if [ "$UV_AVAILABLE" = "1" ]; then
    EXTRA_FLAGS="$EXTRA_FLAGS --uv-available"
fi

# Restore stdin from terminal (stdin is exhausted when piped via curl|sh)
if [ ! -t 0 ] && [ -e /dev/tty ]; then
    "$PYTHON" "$INSTALLER" --pip-cmd "$PIP_CMD" $EXTRA_FLAGS "$@" < /dev/tty
else
    "$PYTHON" "$INSTALLER" --pip-cmd "$PIP_CMD" $EXTRA_FLAGS "$@"
fi
