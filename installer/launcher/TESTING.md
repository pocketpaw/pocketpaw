# PocketPaw Launcher — Testing Guide

Step-by-step instructions to test the desktop launcher on macOS and Windows.

---

## Prerequisites (Both Platforms)

```
Python 3.11+
pip install pyinstaller pystray Pillow
```

Clone the repo and `cd` into the project root:

```bash
git clone https://github.com/pocketpaw/pocketpaw.git
cd pocketpaw
```

---

## 1. Icon Generation

Generates `icon.ico` (Windows) and `icon.icns` (macOS) from `icon.png`.

### macOS

```bash
python installer/launcher/build-launcher/make_icons.py
```

**Verify:**

```bash
ls -la installer/launcher/assets/icon.ico installer/launcher/assets/icon.icns
# Both files should exist and be > 0 bytes
file installer/launcher/assets/icon.icns
# Should print: Apple Icon Image
```

### Windows (PowerShell)

```powershell
python installer\launcher\build-launcher\make_icons.py
```

**Verify:**

```powershell
Test-Path installer\launcher\assets\icon.ico   # True
Test-Path installer\launcher\assets\icon.icns  # True
(Get-Item installer\launcher\assets\icon.ico).Length -gt 0  # True
```

---

## 2. Build the Launcher Binary

### macOS

```bash
python installer/launcher/build-launcher/build.py --version 0.3.0-test
```

**Expected output:**

1. PyInstaller runs and creates `dist/launcher/PocketPaw.app`
2. Ad-hoc code signing runs automatically (`codesign --force --deep --sign -`)
3. DMG is created at `dist/launcher/PocketPaw.dmg` with Applications symlink

**Verify:**

```bash
# .app bundle exists
ls -d dist/launcher/PocketPaw.app

# DMG exists
ls -la dist/launcher/PocketPaw.dmg

# Version was injected into Info.plist
/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" \
  dist/launcher/PocketPaw.app/Contents/Info.plist
# Should print: 0.3.0-test

# Not marked as LSUIElement (should be visible in Dock)
/usr/libexec/PlistBuddy -c "Print LSUIElement" \
  dist/launcher/PocketPaw.app/Contents/Info.plist 2>&1
# Should error (key not present) or print False

# Code signature is valid
codesign --verify --deep dist/launcher/PocketPaw.app
# Should exit 0 (no output = valid)
```

### Windows (PowerShell)

```powershell
python installer\launcher\build-launcher\build.py --version 0.3.0-test
```

**Expected output:**

1. PyInstaller runs and creates `dist\launcher\PocketPaw\PocketPaw.exe`
2. If Inno Setup is installed, creates `dist\launcher\PocketPaw-Setup.exe`

**Verify:**

```powershell
# EXE exists
Test-Path dist\launcher\PocketPaw\PocketPaw.exe  # True

# If Inno Setup was found:
Test-Path dist\launcher\PocketPaw-Setup.exe  # True
```

If Inno Setup is not auto-detected, run it manually:

```powershell
# Install Inno Setup: https://jrsoftware.org/isdl.php
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  /DVERSION=0.3.0-test `
  installer\launcher\build-launcher\pocketpaw.iss
```

---

## 3. Test the DMG (macOS only)

```bash
# Mount the DMG
hdiutil attach dist/launcher/PocketPaw.dmg

# Check the volume contents
ls /Volumes/PocketPaw/
# Should show:
#   PocketPaw.app
#   Applications -> /Applications
```

**Manual test:**

1. Open Finder, navigate to the mounted volume
2. Confirm you see `PocketPaw.app` and an `Applications` folder shortcut
3. Drag `PocketPaw.app` to Applications (drag-to-install UX)
4. Eject the DMG
5. Launch from `/Applications/PocketPaw.app`
6. Should NOT show "damaged app" Gatekeeper error (ad-hoc signed)

```bash
# Unmount when done
hdiutil detach /Volumes/PocketPaw
```

---

## 4. Test the Windows Installer

1. Double-click `PocketPaw-Setup.exe`
2. Verify the install wizard shows correct version in the title bar
3. Check the "Create desktop shortcut" task is offered
4. Check the "Start PocketPaw when Windows starts" task is offered
5. Complete installation with default settings
6. Verify shortcuts created:
   - Start Menu: `PocketPaw > PocketPaw`
   - Desktop: `PocketPaw` (if task was checked)
   - Startup: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\PocketPaw.lnk` (if task was checked)
7. Verify PocketPaw launches after install completes
8. Test uninstall via Settings > Apps > PocketPaw:
   - Should prompt about removing `~/.pocketclaw` config data
   - Click "No" to keep data, verify `%USERPROFILE%\.pocketclaw` still exists
   - Reinstall, uninstall again, click "Yes" to remove data
   - Verify `%USERPROFILE%\.pocketclaw` is deleted

---

## 5. Test the Launcher from Source (No Build Needed)

Useful for rapid iteration without building binaries.

### macOS / Linux

```bash
PYTHONPATH=. python -m installer.launcher --no-browser
```

### Windows

```powershell
$env:PYTHONPATH = "."
python -m installer.launcher --no-browser
```

**What to observe:**

1. First run: splash window appears, progress bar fills, pocketpaw installs into `~/.pocketclaw/venv/`
2. Server starts, system tray icon appears
3. Tray menu shows:
   - `PocketPaw v0.1.0` (disabled, version display)
   - `Open Dashboard` (default double-click action)
   - `Start/Stop Server` (dynamic text)
   - `Restart Server`
   - `Start on Login` (checkable)
   - `Check for Updates`
   - `View Logs...`
   - `Uninstall...`
   - `Quit PocketPaw`
4. Tooltip on hover shows `PocketPaw v0.1.0 — Running on port 8888`

---

## 6. Test Auto-Start

### macOS

```bash
# Enable
PYTHONPATH=. python -m installer.launcher --autostart
# Verify plist exists
ls ~/Library/LaunchAgents/com.pocketpaw.launcher.plist
cat ~/Library/LaunchAgents/com.pocketpaw.launcher.plist
# Should contain RunAtLoad = true

# Disable
PYTHONPATH=. python -m installer.launcher --no-autostart
# Verify plist removed
ls ~/Library/LaunchAgents/com.pocketpaw.launcher.plist 2>&1
# Should say "No such file"
```

### Windows (PowerShell)

```powershell
$env:PYTHONPATH = "."

# Enable
python -m installer.launcher --autostart
# Verify registry key
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name PocketPaw
# Should show the exe path

# Disable
python -m installer.launcher --no-autostart
# Verify key removed
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name PocketPaw 2>$null
# Should return nothing / error
```

### Linux

```bash
# Enable
PYTHONPATH=. python -m installer.launcher --autostart
# Verify .desktop file
cat ~/.config/autostart/pocketpaw.desktop
# Should contain [Desktop Entry] with Exec line

# Disable
PYTHONPATH=. python -m installer.launcher --no-autostart
ls ~/.config/autostart/pocketpaw.desktop 2>&1
# Should say "No such file"
```

### Via Tray Menu

1. Launch PocketPaw (with tray)
2. Right-click tray icon
3. Click "Start on Login" — should get a checkmark
4. Verify the platform-specific entry was created (see paths above)
5. Click "Start on Login" again — checkmark should disappear
6. Verify the entry was removed

---

## 7. Test Uninstaller

### Interactive Mode (Console)

```bash
PYTHONPATH=. python -m installer.launcher --uninstall
```

**What to observe:**

1. Lists all found components with their paths
2. Asks about each: venv, uv, python, logs (default Yes), config, memory, audit (default No)
3. Shows results for each removal
4. If auto-start was enabled, it gets disabled automatically

### Via Tray Menu

1. Launch PocketPaw, right-click tray icon
2. Click "Uninstall..."
3. Server stops, safe components (venv, uv, python, logs) are removed
4. Notification: "Uninstall Complete"
5. Tray icon exits
6. Verify `~/.pocketclaw/config.json` and `~/.pocketclaw/memory/` still exist (preserved)

---

## 8. Test Dev / Branch Install

Test installing from a git branch instead of PyPI. Requires `git` on PATH.

### macOS / Linux

```bash
# Fresh install from dev branch
PYTHONPATH=. python -m installer.launcher --dev --reset --no-tray --no-browser

# Verify it installed from git (not PyPI)
cat ~/.pocketclaw/.dev-mode
# Should show: branch=dev

# Verify pocketpaw is installed
~/.pocketclaw/venv/bin/python -c "import pocketclaw; print(pocketclaw.__version__)"
```

### Windows (PowerShell)

```powershell
$env:PYTHONPATH = "."

# Fresh install from dev branch
python -m installer.launcher --dev --reset --no-tray --no-browser

# Verify dev mode marker
Get-Content $env:USERPROFILE\.pocketclaw\.dev-mode
# Should show: branch=dev

# Verify pocketpaw is installed
& "$env:USERPROFILE\.pocketclaw\venv\Scripts\python.exe" -c "import pocketclaw; print(pocketclaw.__version__)"
```

### Test local editable install

```bash
# macOS / Linux
PYTHONPATH=. python -m installer.launcher --local /path/to/pocketpaw --reset --no-tray --no-browser

# Windows
$env:PYTHONPATH = "."
python -m installer.launcher --local C:\path\to\pocketpaw --reset --no-tray --no-browser
```

### Test switching back to PyPI

```bash
# Reset without --dev clears the dev marker and installs from PyPI
PYTHONPATH=. python -m installer.launcher --reset --no-tray --no-browser

# Verify dev marker is gone
ls ~/.pocketclaw/.dev-mode 2>&1
# Should say "No such file"
```

**What to verify:**
- [ ] `--dev` installs from git, not PyPI
- [ ] `~/.pocketclaw/.dev-mode` marker file is created
- [ ] `--reset` without `--dev` removes the marker and installs from PyPI
- [ ] Tray "Check for Updates" offers "re-pull from branch" in dev mode
- [ ] `--local` does an editable install (code changes reflect without reinstall)

---

## 9. Test PowerShell Installer (Windows)

Open PowerShell and run:

```powershell
# From local file (for testing)
.\installer\install.ps1

# With parameters
.\installer\install.ps1 -NonInteractive -Profile minimal
```

**What to observe:**

1. Banner displays
2. Python 3.11+ detected (or installed via uv/winget)
3. uv detected or installed
4. `installer.py` downloaded from GitHub
5. Interactive installer launches

**Edge cases to test:**

- Run in MSYS/Git Bash: `sh installer/install.sh` should print the PowerShell redirect message instead of a hard error

---

## 10. Test View Logs

1. Launch PocketPaw with tray
2. Right-click tray > "View Logs..."
3. Should open `~/.pocketclaw/logs/launcher.log` in the default text editor
   - macOS: opens in Console.app or TextEdit
   - Windows: opens in Notepad
   - Linux: opens with xdg-open

---

## 11. Run Automated Tests

```bash
# All launcher tests (autostart + uninstall + server + updater)
uv run pytest tests/test_launcher_autostart.py \
              tests/test_launcher_uninstall.py \
              tests/test_launcher_server.py \
              tests/test_launcher_updater.py -v

# Expected: 62 passed
```

---

## 12. CI Workflow Validation

Trigger a manual workflow run to test the full CI pipeline:

```bash
gh workflow run "Build Desktop Launcher" -f version=test-0.0.1
```

**Verify for each platform job (macOS-arm64, macOS-x64, Windows):**

1. Icons are generated successfully
2. PyInstaller build completes
3. Platform-specific post-processing runs:
   - macOS: code signing + DMG creation
   - Windows: Inno Setup installer creation
4. Artifact verification step passes (file exists + size printed)
5. SHA256 checksum is generated and printed in logs
6. Artifacts are uploaded (check the workflow summary page)

---

## Quick Checklist

| # | Test | macOS | Windows | Linux |
|---|------|:-----:|:-------:|:-----:|
| 1 | `make_icons.py` produces `.ico` + `.icns` | [ ] | [ ] | [ ] |
| 2 | `build.py --version X` succeeds | [ ] | [ ] | N/A |
| 3 | DMG mounts, shows app + Applications alias | [ ] | N/A | N/A |
| 4 | Installer wizard works, shortcuts created | N/A | [ ] | N/A |
| 5 | Launcher starts from source (`--no-browser`) | [ ] | [ ] | [ ] |
| 6 | Tray menu shows all items with version | [ ] | [ ] | [ ] |
| 7 | Tooltip updates dynamically | [ ] | [ ] | [ ] |
| 8 | `--autostart` creates platform entry | [ ] | [ ] | [ ] |
| 9 | `--no-autostart` removes it | [ ] | [ ] | [ ] |
| 10 | Tray "Start on Login" toggles correctly | [ ] | [ ] | [ ] |
| 11 | `--uninstall` interactive mode works | [ ] | [ ] | [ ] |
| 12 | Tray "Uninstall..." removes safe components | [ ] | [ ] | [ ] |
| 13 | "View Logs..." opens log file | [ ] | [ ] | [ ] |
| 14 | `--dev` installs from git branch | [ ] | [ ] | [ ] |
| 15 | `--branch X` installs from custom branch | [ ] | [ ] | [ ] |
| 16 | `--local /path` does editable install | [ ] | [ ] | [ ] |
| 17 | `--reset` without `--dev` switches back to PyPI | [ ] | [ ] | [ ] |
| 18 | Dev mode updater re-pulls from branch | [ ] | [ ] | [ ] |
| 19 | `install.ps1` bootstraps correctly | N/A | [ ] | N/A |
| 20 | `install.sh` redirects Windows users to PS1 | N/A | [ ] | N/A |
| 21 | CI workflow produces all 3 artifacts | [ ] | [ ] | N/A |
| 22 | Checksums are generated alongside artifacts | [ ] | [ ] | N/A |
| 23 | Automated tests pass (62 tests) | [ ] | [ ] | [ ] |
