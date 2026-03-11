// Tauri IPC commands for the PocketPaw desktop client.
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
            format!("{}\\.local\\bin", home_str),
            format!("{}\\.cargo\\bin", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python311\\Scripts", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python312\\Scripts", home_str),
            format!("{}\\AppData\\Local\\Programs\\Python\\Python313\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python311\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python312\\Scripts", home_str),
            format!("{}\\AppData\\Roaming\\Python\\Python313\\Scripts", home_str),
        ]
    } else {
        vec![
            format!("{}/.local/bin", home_str),
            format!("{}/.cargo/bin", home_str),
            "/opt/homebrew/bin".to_string(),
            "/opt/homebrew/sbin".to_string(),
            "/usr/local/bin".to_string(),
            "/usr/local/sbin".to_string(),
            format!("{}/Library/Python/3.11/bin", home_str),
            format!("{}/Library/Python/3.12/bin", home_str),
            format!("{}/Library/Python/3.13/bin", home_str),
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

    let candidates = [
        home.join(".local/bin/pocketpaw"),
        home.join(".cargo/bin/pocketpaw"),
        PathBuf::from("/opt/homebrew/bin/pocketpaw"),
        PathBuf::from("/usr/local/bin/pocketpaw"),
    ];

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
#[cfg(windows)]
fn _spawn_installer(profile: &str) -> std::io::Result<std::process::Child> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    let ps_cmd = format!(
        "$tmp = Join-Path $env:TEMP 'pocketpaw_installer.py'; \
         Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py' \
           -OutFile $tmp -UseBasicParsing; \
         python $tmp --non-interactive --profile {} --uv-available --no-launch; \
         Remove-Item $tmp -ErrorAction SilentlyContinue",
        profile
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
/// Downloads installer.py to a temp file and runs it non-interactively,
/// matching the Windows approach. The shell wrapper (install.sh) expects
/// a TTY which isn't available when spawned from Tauri with piped I/O.
#[cfg(not(windows))]
fn _spawn_installer(profile: &str) -> std::io::Result<std::process::Child> {
    let cmd = format!(
        "tmp=$(mktemp /tmp/pocketpaw_installer.XXXXXX.py) && \
         curl -fsSL https://raw.githubusercontent.com/pocketpaw/pocketpaw/main/installer/installer.py -o \"$tmp\" && \
         python3 \"$tmp\" --non-interactive --profile {} --no-launch; \
         rm -f \"$tmp\"",
        profile
    );
    _cmd("sh")
        .args(["-c", &cmd])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

/// Install PocketPaw by spawning a non-interactive installer process.
/// Streams stdout line-by-line via "install-progress" events.
#[tauri::command]
pub async fn install_pocketpaw(app: AppHandle, profile: String) -> Result<bool, String> {
    // Validate profile against allowlist to prevent command injection
    if !["minimal", "recommended", "full"].contains(&profile.as_str()) {
        return Err(format!("Invalid install profile: {}", profile));
    }

    // Run the Python installer directly in non-interactive mode.
    // We avoid the wrapper scripts (install.ps1/install.sh) because they rely on
    // an interactive console ([Console]::OutputEncoding / Rich) which isn't
    // available when spawned headless from Tauri with piped stdout/stderr.
    //
    // Flow: download installer.py to temp dir, run with --non-interactive --profile.
    let child = _spawn_installer(&profile);

    let mut child = child.map_err(|e| format!("Failed to spawn installer: {}", e))?;

    let stdout = child.stdout.take().ok_or("Failed to capture stdout")?;
    let reader = BufReader::new(stdout);

    // Strip ANSI escape sequences from installer output
    let ansi_re = Regex::new(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^\[\]].?")
        .unwrap();

    for line in reader.lines() {
        match line {
            Ok(text) => {
                let clean = ansi_re.replace_all(&text, "").to_string();
                // Skip empty lines after stripping
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

/// Spawn backend process — platform-specific to handle Windows console hiding.
/// Uses CREATE_NO_WINDOW to suppress console + CREATE_NEW_PROCESS_GROUP so the
/// backend survives if the Tauri app exits. DETACHED_PROCESS is avoided because
/// it conflicts with CREATE_NO_WINDOW and can spawn a visible console for child processes.
#[cfg(windows)]
fn _spawn_backend(port_str: &str) -> std::io::Result<std::process::Child> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;

    let path = _augmented_path();
    let flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP;

    Command::new("pocketpaw")
        .args(["serve", "--port", port_str])
        .env("PATH", &path)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .stdin(Stdio::null())
        .creation_flags(flags)
        .spawn()
        .or_else(|_| {
            Command::new("uv")
                .args(["run", "pocketpaw", "serve", "--port", port_str])
                .env("PATH", &path)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .stdin(Stdio::null())
                .creation_flags(flags)
                .spawn()
        })
}

#[cfg(not(windows))]
fn _spawn_backend(port_str: &str) -> std::io::Result<std::process::Child> {
    let path = _augmented_path();

    Command::new("pocketpaw")
        .args(["serve", "--port", port_str])
        .env("PATH", &path)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .or_else(|_| {
            Command::new("uv")
                .args(["run", "pocketpaw", "serve", "--port", port_str])
                .env("PATH", &path)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
        })
}

/// Start the PocketPaw backend as a detached background process on the given port.
/// Returns immediately — frontend should poll check_backend_running to confirm.
#[tauri::command]
pub fn start_pocketpaw_backend(port: u16) -> Result<bool, String> {
    let port_str = port.to_string();

    let result = _spawn_backend(&port_str);

    match result {
        Ok(_) => Ok(true),
        Err(e) => Err(format!("Failed to start backend: {}", e)),
    }
}
