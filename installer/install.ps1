#Requires -Version 5.1
<#
.SYNOPSIS
    PocketPaw Installer for Windows.

.DESCRIPTION
    Bootstraps Python, uv, and downloads the interactive installer.
    Equivalent to install.sh but for native Windows (PowerShell 5.1+).

.PARAMETER NonInteractive
    Run without prompts (accept defaults).

.PARAMETER Profile
    Installation profile: minimal, recommended (default), or full.

.EXAMPLE
    irm https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/install.ps1 | iex
#>

[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [ValidateSet("minimal", "recommended", "full")]
    [string]$Profile = "recommended"
)

$ErrorActionPreference = "Stop"

# ── Banner ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ┌─────────────────────────────────────────┐" -ForegroundColor Magenta
Write-Host "  │  🐾  PocketPaw Installer                │" -ForegroundColor Magenta
Write-Host "  │  The AI agent that runs on your laptop   │" -ForegroundColor Magenta
Write-Host "  └─────────────────────────────────────────┘" -ForegroundColor Magenta
Write-Host ""

# ── Helper Functions ────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "  $msg" }
function Write-Ok($msg) { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  Warn: $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  Error: $msg" -ForegroundColor Red }

function Test-PythonVersion {
    param([string]$Cmd)
    try {
        $ver = & $Cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $parts = $ver.Trim().Split(".")
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        return ($major -ge 3 -and $minor -ge 11)
    } catch {
        return $false
    }
}

function Get-PythonFullVersion {
    param([string]$Cmd)
    try {
        $ver = & $Cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        return $ver.Trim()
    } catch {
        return "unknown"
    }
}

# ── Find Python 3.11+ ──────────────────────────────────────────────────
$Python = $null

# Try common commands
foreach ($cmd in @("python", "python3", "py")) {
    if ($cmd -eq "py") {
        # Windows Python launcher: try py -3
        try {
            $testVer = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $parts = $testVer.Trim().Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                    $Python = "py -3"
                    break
                }
            }
        } catch {}
    } else {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found -and (Test-PythonVersion $cmd)) {
            $Python = $cmd
            break
        }
    }
}

# Cascade 1: Try uv python install
if (-not $Python) {
    Write-Step "Python 3.11+ not found. Attempting to install..."

    # Install uv first
    $uvAvailable = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $uvAvailable = $true
    } else {
        Write-Step "Installing uv (fast Python package manager)..."
        try {
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression 2>$null
            # Refresh PATH
            $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
            if (Get-Command uv -ErrorAction SilentlyContinue) {
                $uvAvailable = $true
                Write-Ok "uv installed"
            }
        } catch {
            Write-Warn "Could not install uv automatically."
        }
    }

    if ($uvAvailable) {
        Write-Step "Installing Python 3.12 via uv..."
        & uv python install 3.12 2>$null
        if ($LASTEXITCODE -eq 0) {
            $uvPython = & uv python find 3.12 2>$null
            if ($uvPython) {
                $Python = $uvPython.Trim()
                Write-Ok "Python 3.12 installed via uv"
            }
        }
    }
}

# Cascade 2: Try winget
if (-not $Python) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Step "Installing Python 3.12 via winget..."
        try {
            winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>$null
            # Refresh PATH after winget install
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "Machine")

            foreach ($cmd in @("python", "python3")) {
                if ((Get-Command $cmd -ErrorAction SilentlyContinue) -and (Test-PythonVersion $cmd)) {
                    $Python = $cmd
                    Write-Ok "Python 3.12 installed via winget"
                    break
                }
            }
        } catch {
            Write-Warn "winget install failed."
        }
    }
}

# Cascade 3: hard exit
if (-not $Python) {
    Write-Err "Python 3.11+ is required but could not be installed."
    Write-Host "       Install manually from: https://www.python.org/downloads/"
    Write-Host "       Or run: winget install Python.Python.3.12"
    exit 1
}

$pyVer = Get-PythonFullVersion $Python
$pyPath = if ($Python -eq "py -3") { (Get-Command py).Source } else { (Get-Command $Python -ErrorAction SilentlyContinue).Source }
Write-Step "Python:  $pyVer ($pyPath)"

# ── Ensure uv is available ──────────────────────────────────────────────
$uvAvailable = $false
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $uvAvailable = $true
} else {
    Write-Step "Installing uv (fast Python package manager)..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression 2>$null
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            $uvAvailable = $true
            Write-Ok "uv installed"
        }
    } catch {
        Write-Warn "Could not install uv."
    }
}

# Determine pip command
$PipCmd = $null
if ($uvAvailable) {
    $PipCmd = "uv pip"
    Write-Step "Installer: uv pip"
} else {
    # Try python -m pip
    if ($Python -eq "py -3") {
        & py -3 -m pip --version 2>$null | Out-Null
    } else {
        & $Python -m pip --version 2>$null | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        $PipCmd = "$Python -m pip"
        Write-Step "Installer: pip"
    } else {
        Write-Err "No package installer found. Install uv or pip first."
        exit 1
    }
}

Write-Host ""

# ── Download installer.py ──────────────────────────────────────────────
$InstallerUrl = "https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py"
$FallbackUrl = "https://raw.githubusercontent.com/pocketpaw/pocketpaw/dev/installer/installer.py"
$TempInstaller = Join-Path $env:TEMP "pocketpaw_installer.py"

# Configure proxy if set
$webParams = @{ Uri = $InstallerUrl; OutFile = $TempInstaller; UseBasicParsing = $true }
if ($env:HTTP_PROXY) {
    $webParams.Proxy = $env:HTTP_PROXY
}

Write-Step "Downloading installer..."
try {
    Invoke-WebRequest @webParams -ErrorAction Stop
} catch {
    Write-Warn "Primary download failed, trying fallback..."
    $webParams.Uri = $FallbackUrl
    try {
        Invoke-WebRequest @webParams -ErrorAction Stop
    } catch {
        Write-Err "Could not download installer."
        Write-Host "       Try manually: $InstallerUrl"
        exit 1
    }
}

# Verify it looks like Python
$firstLine = Get-Content $TempInstaller -TotalCount 1
if ($firstLine -notmatch '^(#|"""|import |from |def |class )') {
    Write-Err "Downloaded file does not look like a Python script."
    Remove-Item $TempInstaller -ErrorAction SilentlyContinue
    exit 1
}

Write-Step "Launching interactive installer..."
Write-Host ""

# ── Run installer ──────────────────────────────────────────────────────
$extraFlags = @("--from-git")
if ($uvAvailable) { $extraFlags += "--uv-available" }
if ($NonInteractive) { $extraFlags += "--non-interactive" }
if ($Profile -ne "recommended") { $extraFlags += "--profile"; $extraFlags += $Profile }

try {
    if ($Python -eq "py -3") {
        & py -3 $TempInstaller --pip-cmd $PipCmd @extraFlags
    } else {
        & $Python $TempInstaller --pip-cmd $PipCmd @extraFlags
    }
} finally {
    Remove-Item $TempInstaller -ErrorAction SilentlyContinue
}
