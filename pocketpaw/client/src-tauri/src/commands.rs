// Tauri IPC commands for the PocketPaw desktop client.
// Updated: 2026-03-12 — Full bootstrap pipeline for fresh machines: installer now
//   finds/installs Python (via uv, winget, brew, apt) before running installer.py.
//   Removed broken --uv-available hardcoding. Read both stdout+stderr so errors
//   are always visible. Backend startup tries venv Python first. Added venv paths
//   to augmented PATH and known binary checks.
// Updated: 2026-03-11 — Fix cross-platform compile: use #[cfg(windows)] instead
//   of cfg!(windows) for Windows-only process creation flags in install_pocketpaw.
// Updated: 2026-03-09 — Fix PATH detection for macOS GUI apps: augment PATH
//   with common bin dirs (~/.local/bin, /opt/homebrew/bin, etc.) since Tauri
//   apps don't inherit shell PATH. Smarter install detection: check config dir,
//   direct binary paths, pip show. Strip ANSI from installer output.
use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

use regex::Regex;
use serde::Serialize;
use tauri::{AppHandle, Emitter};

/// Augment the current PATH with common binary locations that macOS GUI apps miss.
/// Tauri apps launched from Finder/Dock don't source .zshrc/.bashrc, so they get
/// a minimal PATH like /usr/bin:/bin:/usr/sbin:/sbin. This adds the dirs where
/// pip, uv, homebrew, and cargo typically install binaries.
fn _augmented_path() -> String {
    let current = env::var("PATH").unwrap_or_default();
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"));
    let home_str = home.to_string_lossy();

    let separator = if cfg!(windows) { ";" } else { ":" };

    let extra_dirs: Vec<String> = if cfg!(windows) {
        vec![
            // PocketPaw's own venv (where the installer puts it)
            format!("{}\\.pocketpaw\\venv\\Scripts", home_str),
            // PocketPaw's bundled uv
            format!("{}\\.pocketpaw\\uv", home_str),
            format!("{}\\.local\\bin", home_str),
            format!("{}\\.cargo\\bin", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python311\\Scripts", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python312\\Scripts", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python313\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python311\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python312\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python313\\Scripts", home_str),
            // Standard uv install location on Windows
            format!("{}\\AppData\\Local\\uv\\bin", home_str),
            // pipx installs binaries here on Windows
            format!("{}\\AppData\\Local\\pipx\\venvs\\pocketpaw\\Scripts", home_str),
            format!("{}\\.local\\pipx\\venvs\\pocketpaw\\Scripts", home_str),
        ]
    } else {
        vec![
            // PocketPaw's own venv (where the installer puts it)
            format!("{}/.pocketpaw/venv/bin", home_str),
            // PocketPaw's bundled uv
            format!("{}/.pocketpaw/uv", home_str),
            format!("{}/.local/bin", home_str),
            format!("{}/.cargo/bin", home_str),
            "/opt/homebrew/bin".to_string(),
            "/opt/homebrew/sbin".to_string(),
            "/usr/local/bin".to_string(),
            "/usr/local/sbin".to_string(),
            format!("{}/Library/Python/3.11/bin", home_str),
            format!("{}/Library/Python/3.12/bin", home_str),
            format!("{}/Library/Python/3.13/bin", home_str),
            // pipx installs binaries here
            format!("{}/.local/pipx/venvs/pocketpaw/bin", home_str),
        ]
    };

    let mut parts: Vec<&str> = current.split(separator).collect();
    for dir in &extra_dirs {
        if !parts.contains(&dir.as_str()) {
            parts.push(dir);
        }
    }
    parts.join(separator)
}

/// Create a Command with the augmented PATH set.
/// Sets CWD to the home directory to avoid picking up local pyproject.toml.
fn _cmd(program: &str) -> Command {
    let mut cmd = Command::new(program);
    cmd.env("PATH", _augmented_path());
    if let Some(home) = dirs::home_dir() {
        cmd.current_dir(home);
    }
    cmd
}

/// Read the access token from ~/.pocketpaw/access_token
#[tauri::command]
pub fn read_access_token() -> Result<String, String> {
    let home = dirs::home_dir().ok_or("Could not determine home directory")?;
    let token_path = home.join(".pocketpaw").join("access_token");

    fs::read_to_string(&token_path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("Failed to read token: {}", e))
}

/// Return the PocketPaw config directory path
#[tauri::command]
pub fn get_pocketpaw_config_dir() -> Result<String, String> {
    let home = dirs::home_dir().ok_or("Could not determine home directory")?;
    let config_dir = home.join(".pocketpaw");
    Ok(config_dir.to_string_lossy().to_string())
}

/// Check if a backend is running on the given port
#[tauri::command]
pub fn check_backend_running(port: u16) -> Result<bool, String> {
    let addr = format!("127.0.0.1:{}", port);
    match TcpStream::connect_timeout(
        &addr.parse().map_err(|e| format!("Invalid address: {}", e))?,
        Duration::from_secs(2),
    ) {
        Ok(_) => Ok(true),
        Err(_) => Ok(false),
    }
}

/// Check if the backend on the given port is actually PocketPaw by hitting /api/v1/version.
/// Done from Rust to avoid CORS/mixed-content issues in the Tauri webview.
#[tauri::command]
pub fn check_pocketpaw_version(port: u16) -> Result<Option<String>, String> {
    let url = format!("http://127.0.0.1:{}/api/v1/version", port);
    let client = std::net::TcpStream::connect_timeout(
        &format!("127.0.0.1:{}", port)
            .parse()
            .map_err(|e| format!("{}", e))?,
        Duration::from_secs(2),
    );
    if client.is_err() {
        return Ok(None);
    }

    // Use a simple blocking HTTP GET
    let agent = ureq::Agent::new_with_config(
        ureq::config::Config::builder()
            .timeout_global(Some(Duration::from_secs(5)))
            .build(),
    );
    match agent.get(&url).call() {
        Ok(response) => {
            let body: String = response
                .into_body()
                .read_to_string()
                .unwrap_or_default();
            // Parse JSON to extract "version" field
            if let Some(start) = body.find("\"version\"") {
                if let Some(colon) = body[start..].find(':') {
                    let after_colon = &body[start + colon + 1..];
                    let trimmed = after_colon.trim_start();
                    if trimmed.starts_with('"') {
                        if let Some(end) = trimmed[1..].find('"') {
                            return Ok(Some(trimmed[1..1 + end].to_string()));
                        }
                    }
                }
            }
            Ok(None)
        }
        Err(_) => Ok(None),
    }
}

#[derive(Serialize, Clone)]
pub struct InstallStatus {
    pub installed: bool,
    pub has_config_dir: bool,
    pub has_cli: bool,
    pub config_dir: String,
}

/// Check if PocketPaw is installed.
/// Uses augmented PATH to find binaries that macOS GUI apps would miss.
/// Checks: direct binary in PATH → binary at known paths → uv run → pip show
#[tauri::command]
pub fn check_pocketpaw_installed() -> Result<InstallStatus, String> {
    let home = dirs::home_dir().ok_or("Could not determine home directory")?;
    let config_dir = home.join(".pocketpaw");
    let has_config_dir = config_dir.is_dir();

    let has_cli = _check_cli_direct()
        || _check_binary_at_known_paths()
        || _check_cli_via_uv()
        || _check_via_pip();

    Ok(InstallStatus {
        installed: has_config_dir || has_cli,
        has_config_dir,
        has_cli,
        config_dir: config_dir.to_string_lossy().to_string(),
    })
}

/// Check if `pocketpaw` is in the (augmented) PATH
fn _check_cli_direct() -> bool {
    if cfg!(windows) {
        _cmd("where")
            .arg("pocketpaw")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    } else {
        _cmd("which")
            .arg("pocketpaw")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }
}

/// Check common binary installation paths directly (no PATH needed)
fn _check_binary_at_known_paths() -> bool {
    let home = match dirs::home_dir() {
        Some(h) => h,
        None => return false,
    };

    let mut candidates = vec![
        home.join(".local/bin/pocketpaw"),
        home.join(".cargo/bin/pocketpaw"),
    ];

    // Check the installer's venv — pocketpaw binary or the venv Python itself
    if cfg!(windows) {
        candidates.push(home.join(".pocketpaw/venv/Scripts/pocketpaw.exe"));
        candidates.push(home.join(".pocketpaw/venv/Scripts/python.exe"));
    } else {
        candidates.push(home.join(".pocketpaw/venv/bin/pocketpaw"));
        candidates.push(home.join(".pocketpaw/venv/bin/python"));
        candidates.push(PathBuf::from("/opt/homebrew/bin/pocketpaw"));
        candidates.push(PathBuf::from("/usr/local/bin/pocketpaw"));
    }

    candidates.iter().any(|p| p.exists())
}

/// Check if `pocketpaw` is available via `uv run`
/// Uses --no-project --isolated so it won't resolve from a local pyproject.toml
/// or cached virtual environments
fn _check_cli_via_uv() -> bool {
    _cmd("uv")
        .args(["run", "--no-project", "--isolated", "pocketpaw", "--version"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Check if pocketpaw is installed as a pip package
fn _check_via_pip() -> bool {
    // Try pip show (fast, doesn't import anything)
    _cmd("pip")
        .args(["show", "pocketpaw"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
        || _cmd("pip3")
            .args(["show", "pocketpaw"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
}

#[derive(Serialize, Clone)]
pub struct InstallProgress {
    pub line: String,
    pub done: bool,
    pub success: bool,
}

/// Spawn the installer process (Windows variant).
///
/// Full bootstrap pipeline for fresh machines (no Python, no uv):
///   1. Search for Python 3.11+ (python, python3, py -3)
///   2. If missing → install uv → `uv python install 3.12`
///   3. If uv fails → try `winget install Python.Python.3.12`
///   4. If still nothing → print error with download link and exit
///   5. Detect uv availability, only pass --uv-available when true
///   6. Download installer.py and run it with the found Python
///
/// stderr is merged into stdout (`2>&1`) so all output reaches the UI.
#[cfg(windows)]
fn _spawn_installer(profile: &str) -> std::io::Result<std::process::Child> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    // The PowerShell script is a self-contained bootstrap pipeline.
    // Each Write-Host line becomes a log line in the install progress UI.
    let ps_cmd = format!(
        r#"
$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'

# ── Step 1: Find Python 3.11+ ────────────────────────────────────────
$Python = $null

function Test-PyVer($cmd) {{
    try {{
        $out = & $cmd -c "import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')" 2>$null
        if ($LASTEXITCODE -ne 0) {{ return $false }}
        $parts = $out.Trim().Split('.')
        return ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11)
    }} catch {{ return $false }}
}}

foreach ($cmd in @('python', 'python3')) {{
    if ((Get-Command $cmd -ErrorAction SilentlyContinue) -and (Test-PyVer $cmd)) {{
        $Python = $cmd; break
    }}
}}
if (-not $Python) {{
    try {{
        $out = & py -3 -c "import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')" 2>$null
        if ($LASTEXITCODE -eq 0) {{
            $parts = $out.Trim().Split('.')
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {{ $Python = 'py -3' }}
        }}
    }} catch {{}}
}}

# ── Step 2: No Python → install uv → install Python via uv ───────────
if (-not $Python) {{
    Write-Host 'Python 3.11+ not found. Installing automatically...'

    $uvAvail = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {{
        $uvAvail = $true
    }} else {{
        Write-Host 'Installing uv (fast Python package manager)...'
        try {{
            $uvScript = Join-Path $env:TEMP 'uv-install.ps1'
            Invoke-RestMethod 'https://astral.sh/uv/install.ps1' -OutFile $uvScript
            & $uvScript 2>$null
            Remove-Item $uvScript -ErrorAction SilentlyContinue
            $env:PATH = "$env:LOCALAPPDATA\uv\bin;$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
            if (Get-Command uv -ErrorAction SilentlyContinue) {{
                $uvAvail = $true
                Write-Host 'uv installed successfully'
            }}
        }} catch {{
            Write-Host "Warning: could not install uv: $_"
        }}
    }}

    if ($uvAvail) {{
        Write-Host 'Installing Python 3.12 via uv...'
        & uv python install 3.12 2>&1
        if ($LASTEXITCODE -eq 0) {{
            $uvPy = (& uv python find 3.12 2>$null)
            if ($uvPy) {{
                $Python = $uvPy.Trim()
                Write-Host "Python 3.12 installed via uv"
            }}
        }}
    }}
}}

# ── Step 3: Still no Python → try winget ──────────────────────────────
if (-not $Python) {{
    if (Get-Command winget -ErrorAction SilentlyContinue) {{
        Write-Host 'Installing Python 3.12 via winget...'
        try {{
            winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>&1
            $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','User') + ';' + [System.Environment]::GetEnvironmentVariable('PATH','Machine')
            foreach ($cmd in @('python', 'python3')) {{
                if ((Get-Command $cmd -ErrorAction SilentlyContinue) -and (Test-PyVer $cmd)) {{
                    $Python = $cmd
                    Write-Host "Python installed via winget"
                    break
                }}
            }}
        }} catch {{
            Write-Host "Warning: winget install failed: $_"
        }}
    }}
}}

# ── Step 4: Give up ──────────────────────────────────────────────────
if (-not $Python) {{
    Write-Host 'ERROR: Python 3.11+ is required but could not be installed.'
    Write-Host 'Please install Python manually from: https://www.python.org/downloads/'
    Write-Host 'Or run: winget install Python.Python.3.12'
    exit 1
}}

Write-Host "Using Python: $Python"

# ── Step 5: Detect uv for --uv-available flag ────────────────────────
$uvFlag = ''
if (Get-Command uv -ErrorAction SilentlyContinue) {{ $uvFlag = '--uv-available' }}

# ── Step 6: Download and run installer.py ─────────────────────────────
$tmp = Join-Path $env:TEMP 'pocketpaw_installer.py'
Write-Host 'Downloading PocketPaw installer...'
try {{
    Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py' -OutFile $tmp -UseBasicParsing
}} catch {{
    Write-Host 'Primary download failed, trying fallback...'
    try {{
        Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/pocketpaw/pocketpaw/dev/installer/installer.py' -OutFile $tmp -UseBasicParsing
    }} catch {{
        Write-Host "ERROR: Could not download installer: $_"
        exit 1
    }}
}}

$extraFlags = @('--non-interactive', '--profile', '{profile}', '--no-launch')
if ($uvFlag) {{ $extraFlags += $uvFlag }}

if ($Python -eq 'py -3') {{
    & py -3 $tmp @extraFlags 2>&1
}} else {{
    & $Python $tmp @extraFlags 2>&1
}}
$exitCode = $LASTEXITCODE
Remove-Item $tmp -ErrorAction SilentlyContinue

if ($exitCode -ne 0) {{ exit $exitCode }}

# ── Step 7: Install Claude Code CLI if not found ─────────────────────
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {{
    Write-Host 'Installing Claude Code CLI...'
    try {{
        irm https://claude.ai/install.ps1 | iex 2>&1
        # Refresh PATH to pick up new binary
        $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','User') + ';' + [System.Environment]::GetEnvironmentVariable('PATH','Machine')
        if (Get-Command claude -ErrorAction SilentlyContinue) {{
            Write-Host 'Claude Code CLI installed successfully'
        }} else {{
            Write-Host 'Warning: Claude Code CLI install completed but claude not found on PATH'
            Write-Host 'You can install it manually later: npm install -g @anthropic-ai/claude-code'
        }}
    }} catch {{
        Write-Host "Warning: Could not install Claude Code CLI: $_"
        Write-Host 'You can install it manually later: npm install -g @anthropic-ai/claude-code'
    }}
}} else {{
    Write-Host 'Claude Code CLI already installed'
}}

exit 0
"#,
        profile = profile
    );

    Command::new("powershell")
        .args([
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            &ps_cmd,
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
}

/// Spawn the installer process (Unix variant).
///
/// Full bootstrap pipeline for fresh machines:
///   1. Search for python3 with version >= 3.11
///   2. If missing → install uv → `uv python install 3.12`
///   3. If uv fails → try brew (macOS) or apt/dnf (Linux)
///   4. If still nothing → print error with install instructions and exit
///   5. Detect uv availability, only pass --uv-available when true
///   6. Download installer.py and run it
///
/// stderr is merged into stdout (`2>&1`) so all output reaches the UI.
#[cfg(not(windows))]
fn _spawn_installer(profile: &str) -> std::io::Result<std::process::Child> {
    let cmd = format!(
        r#"
set -e

# ── Step 1: Find Python 3.11+ ────────────────────────────────────────
PYTHON=""
check_py_ver() {{
    ver=$("$1" -c "import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')" 2>/dev/null) || return 1
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]
}}

for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && check_py_ver "$cmd"; then
        PYTHON="$cmd"
        break
    fi
done

# ── Step 2: No Python → install uv → install Python via uv ───────────
if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ not found. Installing automatically..."

    UV_AVAIL=false
    if command -v uv >/dev/null 2>&1; then
        UV_AVAIL=true
    else
        echo "Installing uv (fast Python package manager)..."
        if curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh 2>&1; then
            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
            if command -v uv >/dev/null 2>&1; then
                UV_AVAIL=true
                echo "uv installed successfully"
            fi
        else
            echo "Warning: could not install uv"
        fi
    fi

    if [ "$UV_AVAIL" = true ]; then
        echo "Installing Python 3.12 via uv..."
        uv python install 3.12 2>&1
        uv_py=$(uv python find 3.12 2>/dev/null || true)
        if [ -n "$uv_py" ] && check_py_ver "$uv_py"; then
            PYTHON="$uv_py"
            echo "Python 3.12 installed via uv"
        fi
    fi
fi

# ── Step 3: Still no Python → try system package manager ─────────────
if [ -z "$PYTHON" ]; then
    if [ "$(uname)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            echo "Installing Python 3.12 via Homebrew..."
            brew install python@3.12 2>&1 || true
            for cmd in python3 python3.12; do
                if command -v "$cmd" >/dev/null 2>&1 && check_py_ver "$cmd"; then
                    PYTHON="$cmd"
                    echo "Python installed via Homebrew"
                    break
                fi
            done
        fi
    else
        # Linux — try apt or dnf
        if command -v apt-get >/dev/null 2>&1; then
            echo "Installing Python 3 via apt..."
            sudo apt-get update -qq 2>&1 && sudo apt-get install -y -qq python3 python3-venv 2>&1 || true
        elif command -v dnf >/dev/null 2>&1; then
            echo "Installing Python 3 via dnf..."
            sudo dnf install -y python3 2>&1 || true
        fi
        for cmd in python3 python; do
            if command -v "$cmd" >/dev/null 2>&1 && check_py_ver "$cmd"; then
                PYTHON="$cmd"
                echo "Python installed via system package manager"
                break
            fi
        done
    fi
fi

# ── Step 4: Give up ──────────────────────────────────────────────────
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ is required but could not be installed."
    echo "Please install Python manually:"
    echo "  macOS: brew install python@3.12"
    echo "  Ubuntu/Debian: sudo apt install python3"
    echo "  Fedora: sudo dnf install python3"
    echo "  Or download from: https://www.python.org/downloads/"
    exit 1
fi

echo "Using Python: $PYTHON"

# ── Step 5: Detect uv for --uv-available flag ────────────────────────
UV_FLAG=""
if command -v uv >/dev/null 2>&1; then UV_FLAG="--uv-available"; fi

# ── Step 6: Download and run installer.py ─────────────────────────────
tmp=$(mktemp /tmp/pocketpaw_installer.XXXXXX.py)
echo "Downloading PocketPaw installer..."
if ! curl -fsSL https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py -o "$tmp" 2>/dev/null; then
    echo "Primary download failed, trying fallback..."
    if ! curl -fsSL https://raw.githubusercontent.com/pocketpaw/pocketpaw/dev/installer/installer.py -o "$tmp" 2>/dev/null; then
        echo "ERROR: Could not download installer."
        rm -f "$tmp"
        exit 1
    fi
fi

export PYTHONIOENCODING=utf-8
"$PYTHON" "$tmp" --non-interactive --profile {profile} --no-launch $UV_FLAG 2>&1
rc=$?
rm -f "$tmp"

if [ $rc -ne 0 ]; then exit $rc; fi

# ── Step 7: Install Claude Code CLI if not found ─────────────────────
if ! command -v claude >/dev/null 2>&1; then
    echo "Installing Claude Code CLI..."
    if curl -fsSL https://claude.ai/install.sh 2>/dev/null | bash 2>&1; then
        export PATH="$HOME/.local/bin:$HOME/.claude/local/bin:$PATH"
        if command -v claude >/dev/null 2>&1; then
            echo "Claude Code CLI installed successfully"
        else
            echo "Warning: Claude Code CLI install completed but claude not found on PATH"
            echo "You can install it manually later: npm install -g @anthropic-ai/claude-code"
        fi
    else
        echo "Warning: Could not install Claude Code CLI"
        echo "You can install it manually later: npm install -g @anthropic-ai/claude-code"
    fi
else
    echo "Claude Code CLI already installed"
fi

exit 0
"#,
        profile = profile
    );

    _cmd("sh")
        .args(["-c", &cmd])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

/// Install PocketPaw by spawning a non-interactive installer process.
/// Streams both stdout and stderr line-by-line via "install-progress" events.
#[tauri::command]
pub async fn install_pocketpaw(app: AppHandle, profile: String) -> Result<bool, String> {
    // Validate profile against allowlist to prevent command injection
    if !["minimal", "recommended", "full"].contains(&profile.as_str()) {
        return Err(format!("Invalid install profile: {}", profile));
    }

    // Spawn the installer with the full bootstrap pipeline.
    // The pipeline handles: find Python → install uv → install Python → download
    // installer.py → run it. stderr is merged into stdout via 2>&1 in the scripts.
    let child = _spawn_installer(&profile);

    let mut child = child.map_err(|e| format!("Failed to spawn installer: {}", e))?;

    // Read both stdout and stderr — spawn threads for each stream so neither blocks.
    let stdout = child.stdout.take().ok_or("Failed to capture stdout")?;
    let stderr = child.stderr.take();

    let app_clone = app.clone();
    let ansi_pattern = r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^\[\]].?";

    // Spawn stderr reader thread (if available) to forward errors to the UI
    let stderr_handle = stderr.map(|se| {
        let app_se = app_clone.clone();
        let re = Regex::new(ansi_pattern).unwrap();
        std::thread::spawn(move || {
            let reader = BufReader::new(se);
            for line in reader.lines().map_while(Result::ok) {
                let clean = re.replace_all(&line, "").to_string();
                if clean.trim().is_empty() {
                    continue;
                }
                let _ = app_se.emit(
                    "install-progress",
                    InstallProgress {
                        line: clean,
                        done: false,
                        success: false,
                    },
                );
            }
        })
    });

    // Read stdout on the current thread
    let ansi_re = Regex::new(ansi_pattern).unwrap();
    let reader = BufReader::new(stdout);

    for line in reader.lines() {
        match line {
            Ok(text) => {
                let clean = ansi_re.replace_all(&text, "").to_string();
                if clean.trim().is_empty() {
                    continue;
                }
                let _ = app.emit(
                    "install-progress",
                    InstallProgress {
                        line: clean,
                        done: false,
                        success: false,
                    },
                );
            }
            Err(_) => break,
        }
    }

    // Wait for stderr thread to finish
    if let Some(handle) = stderr_handle {
        let _ = handle.join();
    }

    let status = child
        .wait()
        .map_err(|e| format!("Failed to wait for installer: {}", e))?;
    let success = status.success();

    let _ = app.emit(
        "install-progress",
        InstallProgress {
            line: if success {
                "Installation complete!".to_string()
            } else {
                "Installation failed.".to_string()
            },
            done: true,
            success,
        },
    );

    Ok(success)
}

/// Build a backend Command with augmented PATH and home CWD.
/// Sets CWD to home directory to avoid picking up local pyproject.toml.
fn _backend_cmd(program: &str) -> Command {
    let mut cmd = Command::new(program);
    cmd.env("PATH", _augmented_path());
    if let Some(home) = dirs::home_dir() {
        cmd.current_dir(home);
    }
    cmd
}

/// Try spawning the backend with multiple strategies in order:
/// 1. Venv Python (`~/.pocketpaw/venv/.../python -m pocketpaw`) — most reliable post-install
/// 2. `pocketpaw serve` (direct binary in PATH)
/// 3. `uv run --no-project pocketpaw serve` (uv-managed)
/// 4. `python -m pocketpaw serve` / `python3 -m pocketpaw serve` (system Python)
/// Returns (Child, strategy_name) on success, or a combined error message.
fn _try_spawn_backend(
    port_str: &str,
    #[cfg(windows)] flags: u32,
) -> Result<(std::process::Child, &'static str), String> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"));

    // Check for the venv Python that the installer creates
    let venv_python = if cfg!(windows) {
        home.join(".pocketpaw").join("venv").join("Scripts").join("python.exe")
    } else {
        home.join(".pocketpaw").join("venv").join("bin").join("python")
    };

    // Build strategy list: (program, args_before_serve, label)
    let venv_py_str = venv_python.to_string_lossy().to_string();
    let mut strategies: Vec<(&str, Vec<&str>, &str)> = Vec::new();

    // Venv Python first — this is where the installer puts pocketpaw
    if venv_python.exists() {
        strategies.push((&venv_py_str, vec!["-m", "pocketpaw"], "venv python -m pocketpaw"));
    }

    if cfg!(windows) {
        strategies.extend([
            ("pocketpaw", vec![], "pocketpaw"),
            ("uv", vec!["run", "--no-project", "pocketpaw"], "uv run pocketpaw"),
            ("python", vec!["-m", "pocketpaw"], "python -m pocketpaw"),
            ("py", vec!["-m", "pocketpaw"], "py -m pocketpaw"),
        ]);
    } else {
        strategies.extend([
            ("pocketpaw", vec![], "pocketpaw"),
            ("uv", vec!["run", "--no-project", "pocketpaw"], "uv run pocketpaw"),
            ("python3", vec!["-m", "pocketpaw"], "python3 -m pocketpaw"),
            ("python", vec!["-m", "pocketpaw"], "python -m pocketpaw"),
        ]);
    }

    let mut errors: Vec<String> = Vec::new();

    for (program, prefix_args, label) in &strategies {
        let mut cmd = _backend_cmd(program);
        for arg in prefix_args {
            cmd.arg(arg);
        }
        cmd.args(["serve", "--port", port_str]);
        cmd.stdout(Stdio::null())
            .stderr(Stdio::null())
            .stdin(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(flags);
        }
        match cmd.spawn() {
            Ok(child) => return Ok((child, label)),
            Err(e) => {
                errors.push(format!("{}: {}", label, e));
            }
        }
    }

    Err(format!(
        "All backend start methods failed:\n{}",
        errors.join("\n")
    ))
}

/// Spawn backend process — platform-specific to handle Windows console hiding.
/// Uses CREATE_NO_WINDOW to suppress console + CREATE_NEW_PROCESS_GROUP so the
/// backend survives if the Tauri app exits. DETACHED_PROCESS is avoided because
/// it conflicts with CREATE_NO_WINDOW and can spawn a visible console for child processes.
#[cfg(windows)]
fn _spawn_backend(port_str: &str) -> Result<std::process::Child, String> {
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
    let flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP;

    _try_spawn_backend(port_str, flags).map(|(child, _)| child)
}

#[cfg(not(windows))]
fn _spawn_backend(port_str: &str) -> Result<std::process::Child, String> {
    _try_spawn_backend(port_str).map(|(child, _)| child)
}

/// Start the PocketPaw backend as a detached background process on the given port.
/// Returns immediately — frontend should poll check_backend_running to confirm.
/// After spawning, waits briefly and checks if the process exited immediately
/// (e.g. due to missing dependencies or config errors).
#[tauri::command]
pub fn start_pocketpaw_backend(port: u16) -> Result<bool, String> {
    let port_str = port.to_string();

    let mut child = _spawn_backend(&port_str)?;

    // Give the process a moment to crash if it's going to
    std::thread::sleep(Duration::from_millis(500));

    // Check if the process already exited (immediate crash)
    match child.try_wait() {
        Ok(Some(status)) if !status.success() => {
            Err(format!(
                "Backend process exited immediately with code {}. \
                 Try running 'pocketpaw serve' in a terminal to see the error.",
                status.code().unwrap_or(-1)
            ))
        }
        Ok(Some(_)) => {
            // Exited with success code 0 — unusual but not an error
            Ok(true)
        }
        Ok(None) => {
            // Still running — good
            Ok(true)
        }
        Err(e) => {
            // Could not check status, assume it's running
            eprintln!("Warning: could not check backend process status: {}", e);
            Ok(true)
        }
    }
}
