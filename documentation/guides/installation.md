# Installation

## Quick install

### macOS / Linux

```bash
curl -fsSL https://pocketpaw.xyz/install.sh | sh
```

### Windows (PowerShell)

```powershell
irm https://pocketpaw.xyz/install.ps1 | iex
```

Both scripts auto-detect your Python version, install `uv` if needed, and launch an interactive installer that walks you through setup.

## pip install

```bash
pip install pocketpaw
pocketpaw
```

## Other methods

```bash
# Isolated install (recommended if you use multiple Python projects)
pipx install pocketpaw && pocketpaw

# Run without installing
uvx pocketpaw

# From source
git clone https://github.com/pocketpaw/pocketpaw.git
cd pocketpaw
uv run pocketpaw
```

## Optional extras

Install only the channel adapters and integrations you need:

```bash
pip install pocketpaw[discord]             # Discord support
pip install pocketpaw[slack]               # Slack support
pip install pocketpaw[whatsapp-personal]   # WhatsApp Personal (QR scan)
pip install pocketpaw[image]               # Image generation (Google Gemini)
pip install pocketpaw[memory]              # Mem0 semantic memory
pip install pocketpaw[matrix]              # Matrix support
pip install pocketpaw[teams]               # Microsoft Teams support
pip install pocketpaw[gchat]               # Google Chat support
pip install pocketpaw[mcp]                 # MCP server support
pip install pocketpaw[all]                 # Everything
```

## Requirements

- Python 3.11+
- macOS, Windows, or Linux
- No Docker required

The installer handles Python and package manager setup automatically. If you already have Python 3.11+ and pip/uv, you can skip straight to `pip install pocketpaw`.

## What the installer does

1. Finds Python 3.11+ (or installs it via uv/Homebrew/apt/winget)
2. Installs `uv` if not present (fast package manager, handles PEP 668 transparently)
3. Downloads and runs the interactive installer
4. Asks you to pick an install profile (minimal, recommended, or full)
5. Installs `pocketpaw` and opens the web dashboard

No sudo required on macOS. On Linux, system package installs (apt/dnf) may prompt for sudo.
