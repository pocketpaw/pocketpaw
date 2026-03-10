use serde::Serialize;
use std::process::Command;

#[derive(Debug, Clone, Serialize)]
pub struct ActiveContext {
    pub app_name: String,
    pub window_title: String,
    pub file_path: Option<String>,
    pub icon: String,
}

impl Default for ActiveContext {
    fn default() -> Self {
        Self {
            app_name: String::new(),
            window_title: String::new(),
            file_path: None,
            icon: "🐾".to_string(),
        }
    }
}

#[tauri::command]
pub fn get_active_context() -> Result<ActiveContext, String> {
    #[cfg(target_os = "linux")]
    {
        get_active_context_linux()
    }
    #[cfg(target_os = "macos")]
    {
        get_active_context_macos()
    }
    #[cfg(target_os = "windows")]
    {
        get_active_context_windows()
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    {
        Ok(ActiveContext::default())
    }
}

#[cfg(target_os = "linux")]
fn get_active_context_linux() -> Result<ActiveContext, String> {
    // Get active window ID via xdotool
    let window_id = Command::new("xdotool")
        .arg("getactivewindow")
        .output()
        .map_err(|e| format!("xdotool failed: {}", e))?;

    if !window_id.status.success() {
        return Ok(ActiveContext::default());
    }

    let wid = String::from_utf8_lossy(&window_id.stdout).trim().to_string();
    if wid.is_empty() {
        return Ok(ActiveContext::default());
    }

    // Get window name
    let window_name = Command::new("xdotool")
        .args(["getwindowname", &wid])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    // Get PID
    let pid_output = Command::new("xdotool")
        .args(["getwindowpid", &wid])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    // Get process name from PID
    let app_name = if !pid_output.is_empty() {
        let comm_path = format!("/proc/{}/comm", pid_output);
        std::fs::read_to_string(&comm_path)
            .map(|s| s.trim().to_string())
            .unwrap_or_default()
    } else {
        String::new()
    };

    // Try to get working directory from PID
    let file_path = if !pid_output.is_empty() {
        let cwd_path = format!("/proc/{}/cwd", pid_output);
        std::fs::read_link(&cwd_path)
            .ok()
            .map(|p| p.to_string_lossy().to_string())
    } else {
        None
    };

    let icon = guess_icon(&app_name, &window_name);

    Ok(ActiveContext {
        app_name,
        window_title: window_name,
        file_path,
        icon,
    })
}

#[cfg(target_os = "macos")]
fn get_active_context_macos() -> Result<ActiveContext, String> {
    // Use osascript to get frontmost app
    let output = Command::new("osascript")
        .args([
            "-e",
            r#"tell application "System Events" to get {name, title of first window} of first application process whose frontmost is true"#,
        ])
        .output()
        .map_err(|e| format!("osascript failed: {}", e))?;

    let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let parts: Vec<&str> = raw.splitn(2, ", ").collect();
    let app_name = parts.first().unwrap_or(&"").to_string();
    let window_title = parts.get(1).unwrap_or(&"").to_string();

    let icon = guess_icon(&app_name, &window_title);

    Ok(ActiveContext {
        app_name,
        window_title,
        file_path: None,
        icon,
    })
}

#[cfg(target_os = "windows")]
fn get_active_context_windows() -> Result<ActiveContext, String> {
    // Basic fallback for Windows — full implementation would use Win32 API
    Ok(ActiveContext::default())
}

fn guess_icon(app_name: &str, window_title: &str) -> String {
    let lower_app = app_name.to_lowercase();
    let lower_title = window_title.to_lowercase();

    if lower_app.contains("code") || lower_app.contains("vim") || lower_app.contains("neovim") {
        return "💻".to_string();
    }
    if lower_app.contains("firefox")
        || lower_app.contains("chrome")
        || lower_app.contains("brave")
        || lower_app.contains("safari")
    {
        return "🌐".to_string();
    }
    if lower_app.contains("word") || lower_title.contains(".docx") || lower_title.contains(".doc")
    {
        return "📄".to_string();
    }
    if lower_app.contains("excel")
        || lower_title.contains(".xlsx")
        || lower_title.contains(".csv")
    {
        return "📊".to_string();
    }
    if lower_app.contains("terminal")
        || lower_app.contains("alacritty")
        || lower_app.contains("kitty")
        || lower_app.contains("wezterm")
        || lower_app.contains("konsole")
    {
        return "⌨️".to_string();
    }
    if lower_app.contains("slack") || lower_app.contains("discord") || lower_app.contains("teams")
    {
        return "💬".to_string();
    }
    if lower_app.contains("figma") || lower_app.contains("gimp") || lower_app.contains("inkscape")
    {
        return "🎨".to_string();
    }
    if lower_app.contains("file") || lower_app.contains("nautilus") || lower_app.contains("dolphin")
    {
        return "📂".to_string();
    }

    "🐾".to_string()
}
