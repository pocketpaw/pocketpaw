// Tauri IPC commands for the PocketPaw desktop client.
// Updated: 2026-03-09 — Improve install detection: also check `uv run pocketpaw`
//   as fallback when CLI not directly in PATH. Strip ANSI escape codes from
//   installer output before emitting to frontend. Previous fixes: #[cfg(windows)]
//   for cross-platform build, unused `_profile` param.
use std::fs;
use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::process::{Command, Stdio};
use std::time::Duration;

use regex::Regex;
use serde::Serialize;
use tauri::{AppHandle, Emitter};

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

#[derive(Serialize, Clone)]
pub struct InstallStatus {
    pub installed: bool,
    pub has_config_dir: bool,
    pub has_cli: bool,
    pub config_dir: String,
}

/// Check if PocketPaw is installed (config dir + CLI in PATH or via uv)
#[tauri::command]
pub fn check_pocketpaw_installed() -> Result<InstallStatus, String> {
    let home = dirs::home_dir().ok_or("Could not determine home directory")?;
    let config_dir = home.join(".pocketpaw");
    let has_config_dir = config_dir.is_dir();

    // Check direct CLI first, then fall back to `uv run pocketpaw --version`
    let has_cli = _check_cli_direct() || _check_cli_via_uv();

    Ok(InstallStatus {
        installed: has_config_dir && has_cli,
        has_config_dir,
        has_cli,
        config_dir: config_dir.to_string_lossy().to_string(),
    })
}

/// Check if `pocketpaw` is directly in PATH
fn _check_cli_direct() -> bool {
    if cfg!(windows) {
        Command::new("where")
            .arg("pocketpaw")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    } else {
        Command::new("which")
            .arg("pocketpaw")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }
}

/// Check if `pocketpaw` is available via `uv run`
fn _check_cli_via_uv() -> bool {
    Command::new("uv")
        .args(["run", "pocketpaw", "--version"])
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

/// Install PocketPaw by spawning a non-interactive installer process.
/// Streams stdout line-by-line via "install-progress" events.
#[tauri::command]
pub async fn install_pocketpaw(app: AppHandle, _profile: String) -> Result<bool, String> {
    let child = if cfg!(windows) {
        Command::new("powershell")
            .args([
                "-NoExit",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "iwr -useb https://pocketpaw.xyz/install.ps1 | iex",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
    } else {
        Command::new("sh")
            .args([
                "-c",
                "curl -fsSL https://pocketpaw.xyz/install.sh | sh",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
    };

    let mut child = child.map_err(|e| format!("Failed to spawn installer: {}", e))?;

    let stdout = child.stdout.take().ok_or("Failed to capture stdout")?;
    let reader = BufReader::new(stdout);

    // Strip ANSI escape sequences from installer output
    let ansi_re = Regex::new(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[\]].?")
        .unwrap();

    for line in reader.lines() {
        match line {
            Ok(text) => {
                let clean = ansi_re.replace_all(&text, "").to_string();
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
#[cfg(windows)]
fn _spawn_backend(port_str: &str) -> std::io::Result<std::process::Child> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    const DETACHED_PROCESS: u32 = 0x00000008;

    Command::new("pocketpaw")
        .args(["serve", "--port", port_str])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS)
        .spawn()
        .or_else(|_| {
            Command::new("uv")
                .args(["run", "pocketpaw", "serve", "--port", port_str])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS)
                .spawn()
        })
}

#[cfg(not(windows))]
fn _spawn_backend(port_str: &str) -> std::io::Result<std::process::Child> {
    Command::new("pocketpaw")
        .args(["serve", "--port", port_str])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .or_else(|_| {
            Command::new("uv")
                .args(["run", "pocketpaw", "serve", "--port", port_str])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
        })
}

/// Start the PocketPaw backend as a detached process on the given port.
/// Returns immediately — frontend should poll check_backend_running to confirm.
#[tauri::command]
pub fn start_pocketpaw_backend(port: u16) -> Result<bool, String> {
    let port_str = port.to_string();

    // Try direct `pocketpaw serve` first, then fall back to `uv run pocketpaw serve`.
    // On Windows, use creation_flags to hide the console window.
    let result = _spawn_backend(&port_str);

    match result {
        Ok(_) => Ok(true),
        Err(e) => Err(format!("Failed to start backend: {}", e)),
    }
}
