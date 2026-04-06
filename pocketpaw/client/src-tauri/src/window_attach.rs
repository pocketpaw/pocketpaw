use serde::{Deserialize, Serialize};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::{AppHandle, Emitter, Manager};

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct WindowRect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct NativeWindowId(pub i64);

#[derive(Debug, Clone, Serialize)]
pub struct AttachInfo {
    pub app_name: String,
    pub window_title: String,
    pub original_rect: WindowRect,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AttachMode {
    Auto,
    Docked,
    Disabled,
}

/// Payload emitted as `"attach-changed"` Tauri event.
#[derive(Debug, Clone, Serialize)]
pub struct AttachChangedPayload {
    pub mode: AttachMode,
    pub attached: bool,
    pub app_name: String,
    pub window_title: String,
}

/// Payload emitted as `"attach-error"` Tauri event.
#[derive(Debug, Clone, Serialize)]
pub struct AttachErrorPayload {
    pub message: String,
}

// ---------------------------------------------------------------------------
// Managed state
// ---------------------------------------------------------------------------

pub struct WindowAttachState {
    pub mode: Mutex<AttachMode>,
    pub current_attach: Mutex<Option<(NativeWindowId, AttachInfo)>>,
    pub poll_stop: Arc<AtomicBool>,
    pub last_fg_change: Mutex<Option<std::time::Instant>>,
    pub own_pid: u32,
}

impl WindowAttachState {
    pub fn new() -> Self {
        Self {
            mode: Mutex::new(AttachMode::Docked),
            current_attach: Mutex::new(None),
            poll_stop: Arc::new(AtomicBool::new(false)),
            last_fg_change: Mutex::new(None),
            own_pid: std::process::id(),
        }
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn set_attach_mode(app: AppHandle, mode: AttachMode) -> Result<(), String> {
    let state = app.state::<WindowAttachState>();
    let old_mode = {
        let mut m = state.mode.lock().unwrap();
        let old = *m;
        *m = mode;
        old
    };

    // If switching away from Auto, restore attached window
    if old_mode == AttachMode::Auto && mode != AttachMode::Auto {
        restore_current_target(&app);
    }

    let _ = app.emit(
        "attach-changed",
        AttachChangedPayload {
            mode,
            attached: false,
            app_name: String::new(),
            window_title: String::new(),
        },
    );

    Ok(())
}

#[tauri::command]
pub fn get_attach_mode(app: AppHandle) -> AttachMode {
    let state = app.state::<WindowAttachState>();
    let mode = *state.mode.lock().unwrap();
    mode
}

#[tauri::command]
pub fn get_attach_info(app: AppHandle) -> Option<AttachInfo> {
    let state = app.state::<WindowAttachState>();
    let result = state
        .current_attach
        .lock()
        .unwrap()
        .as_ref()
        .map(|(_, info)| info.clone());
    result
}

#[tauri::command]
pub fn detach_side_panel(app: AppHandle) -> Result<(), String> {
    restore_current_target(&app);
    let state = app.state::<WindowAttachState>();
    *state.mode.lock().unwrap() = AttachMode::Docked;

    let _ = app.emit(
        "attach-changed",
        AttachChangedPayload {
            mode: AttachMode::Docked,
            attached: false,
            app_name: String::new(),
            window_title: String::new(),
        },
    );

    Ok(())
}

// ---------------------------------------------------------------------------
// Public helpers (called from side_panel.rs)
// ---------------------------------------------------------------------------

/// Attempt to attach to the current foreground window. Called when the side
/// panel is shown in Auto mode.
pub fn try_attach_to_foreground(app: &AppHandle) {
    let state = app.state::<WindowAttachState>();
    if *state.mode.lock().unwrap() != AttachMode::Auto {
        return;
    }

    let own_pid = state.own_pid;
    let panel_width = get_panel_width(app);

    if let Some((wid, app_name, window_title)) = get_foreground_window(own_pid) {
        if is_window_fullscreen(wid) {
            return;
        }
        if let Some(rect) = get_window_rect(wid) {
            attach_to_window(app, wid, &app_name, &window_title, rect, panel_width);
        }
    }
}

/// Restore the currently attached window and clear state. Called when the side
/// panel is hidden.
pub fn restore_and_detach(app: &AppHandle) {
    restore_current_target(app);
}

// ---------------------------------------------------------------------------
// Poll loop
// ---------------------------------------------------------------------------

pub fn start_poll_loop(app: AppHandle) {
    let state = app.state::<WindowAttachState>();
    let stop_flag = state.poll_stop.clone();
    // Reset stop flag in case of restart
    stop_flag.store(false, Ordering::SeqCst);

    // Use a plain OS thread — Win32 FFI is blocking and doesn't play well
    // with the tokio async runtime.
    std::thread::Builder::new()
        .name("window-attach-poll".into())
        .spawn(move || {
            let interval = std::time::Duration::from_millis(250);

            loop {
                if stop_flag.load(Ordering::SeqCst) {
                    break;
                }

                std::thread::sleep(interval);

                if stop_flag.load(Ordering::SeqCst) {
                    break;
                }

                poll_tick(&app);
            }
        })
        .expect("failed to spawn window-attach-poll thread");
}

fn poll_tick(app: &AppHandle) {
    let state = app.state::<WindowAttachState>();
    let mode = *state.mode.lock().unwrap();
    if mode != AttachMode::Auto {
        return;
    }

    // Check if the side panel is visible
    let panel_visible = app
        .get_webview_window("sidepanel")
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);
    if !panel_visible {
        return;
    }

    let own_pid = state.own_pid;
    let panel_width = get_panel_width(app);

    let fg = get_foreground_window(own_pid);

    let current = state.current_attach.lock().unwrap().clone();

    // Extract current target info for comparison
    let cur_wid = current.as_ref().map(|(id, _)| *id);
    let cur_info = current.as_ref().map(|(_, info)| info.clone());

    match (&fg, cur_wid) {
        // Same window — check if it moved/resized
        (Some((wid, _, _)), Some(cid)) if *wid == cid => {
            let info = cur_info.unwrap();
            if let Some(rect) = get_window_rect(*wid) {
                // Check if the window was resized or moved by the user.
                let expected_width = info.original_rect.width.saturating_sub(panel_width);
                let expected_x = info.original_rect.x;
                let expected_y = info.original_rect.y;
                let expected_height = info.original_rect.height;

                if rect.x != expected_x
                    || rect.y != expected_y
                    || rect.width != expected_width
                    || rect.height != expected_height
                {
                    // The target window moved or was resized by the user.
                    let new_original = if rect.width != expected_width
                        || rect.height != expected_height
                    {
                        // User resized — use current rect as new original
                        rect
                    } else {
                        // User moved — update position in original rect
                        WindowRect {
                            x: rect.x,
                            y: rect.y,
                            width: info.original_rect.width,
                            height: info.original_rect.height,
                        }
                    };
                    let app_name = info.app_name.clone();
                    let window_title = info.window_title.clone();
                    attach_to_window(
                        app,
                        *wid,
                        &app_name,
                        &window_title,
                        new_original,
                        panel_width,
                    );
                }
                // Position the panel adjacent to the target
                reposition_panel(app, &rect, panel_width);
            } else {
                // Window gone — clear attachment
                let mut attach = state.current_attach.lock().unwrap();
                *attach = None;
                emit_detached(app);
            }
        }

        // Different foreground window — debounce 500ms before switching
        (Some((wid, app_name, window_title)), _) => {
            let wid = *wid;
            let app_name = app_name.clone();
            let window_title = window_title.clone();

            let mut last_fg = state.last_fg_change.lock().unwrap();
            let now = std::time::Instant::now();

            match *last_fg {
                Some(ts) if now.duration_since(ts).as_millis() >= 500 => {
                    // Debounce elapsed — switch target
                    *last_fg = None;
                    drop(last_fg);

                    if is_window_fullscreen(wid) {
                        return;
                    }

                    // Restore old target
                    restore_current_target(app);

                    if let Some(rect) = get_window_rect(wid) {
                        attach_to_window(app, wid, &app_name, &window_title, rect, panel_width);
                    }
                }
                Some(_) => {
                    // Still debouncing — do nothing
                }
                None => {
                    // Start debounce timer
                    *last_fg = Some(now);
                }
            }
        }

        // No foreground window (e.g. desktop) — just keep current state
        (None, _) => {}
    }
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

fn get_panel_width(app: &AppHandle) -> u32 {
    let sp_state = app.state::<crate::side_panel::SidePanelState>();
    let collapsed = *sp_state.collapsed.lock().unwrap();
    if collapsed {
        48
    } else {
        *sp_state.expanded_width.lock().unwrap() as u32
    }
}

fn attach_to_window(
    app: &AppHandle,
    wid: NativeWindowId,
    app_name: &str,
    window_title: &str,
    original_rect: WindowRect,
    panel_width: u32,
) {
    let state = app.state::<WindowAttachState>();

    // Shrink the target window
    let shrunk = WindowRect {
        x: original_rect.x,
        y: original_rect.y,
        width: original_rect.width.saturating_sub(panel_width),
        height: original_rect.height,
    };

    if !set_window_rect(wid, shrunk) {
        // Failed to resize (e.g., elevated window) — emit error, stay docked
        let _ = app.emit(
            "attach-error",
            AttachErrorPayload {
                message: format!("Cannot resize {app_name} — it may be elevated or protected"),
            },
        );
        return;
    }

    // Position the panel in the freed space
    reposition_panel(
        app,
        &shrunk,
        panel_width,
    );

    // Save attach state
    let info = AttachInfo {
        app_name: app_name.to_string(),
        window_title: window_title.to_string(),
        original_rect,
    };
    *state.current_attach.lock().unwrap() = Some((wid, info));

    let _ = app.emit(
        "attach-changed",
        AttachChangedPayload {
            mode: AttachMode::Auto,
            attached: true,
            app_name: app_name.to_string(),
            window_title: window_title.to_string(),
        },
    );
}

fn reposition_panel(app: &AppHandle, target_rect: &WindowRect, panel_width: u32) {
    if let Some(panel) = app.get_webview_window("sidepanel") {
        // Panel goes to the right edge of the (shrunk) target window
        let panel_x = target_rect.x + target_rect.width as i32;
        let panel_y = target_rect.y;
        let panel_height = target_rect.height;

        let _ = panel.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
            x: panel_x,
            y: panel_y,
        }));
        let _ = panel.set_size(tauri::Size::Physical(tauri::PhysicalSize {
            width: panel_width,
            height: panel_height,
        }));
    }
}

fn restore_current_target(app: &AppHandle) {
    let state = app.state::<WindowAttachState>();
    let attach = state.current_attach.lock().unwrap().take();
    if let Some((wid, info)) = attach {
        set_window_rect(wid, info.original_rect);
    }
}

fn emit_detached(app: &AppHandle) {
    let _ = app.emit(
        "attach-changed",
        AttachChangedPayload {
            mode: AttachMode::Auto,
            attached: false,
            app_name: String::new(),
            window_title: String::new(),
        },
    );
}

// ===========================================================================
// Platform: Windows
// ===========================================================================

#[cfg(target_os = "windows")]
mod platform {
    use super::{NativeWindowId, WindowRect};
    use windows::Win32::Foundation::{HWND, RECT};
    use windows::Win32::Graphics::Gdi::MonitorFromWindow;
    use windows::Win32::Graphics::Gdi::MONITOR_DEFAULTTONEAREST;
    use windows::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowLongW, GetWindowRect, GetWindowTextLengthW, GetWindowTextW,
        GetWindowThreadProcessId, IsIconic, IsWindowVisible, SetWindowPos, GWL_EXSTYLE,
        HWND_TOP, SWP_NOACTIVATE, SWP_NOZORDER, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
    };

    fn hwnd_to_id(hwnd: HWND) -> NativeWindowId {
        NativeWindowId(hwnd.0 as i64)
    }

    fn id_to_hwnd(id: NativeWindowId) -> HWND {
        HWND(id.0 as *mut _)
    }

    fn get_window_text(hwnd: HWND) -> String {
        unsafe {
            let len = GetWindowTextLengthW(hwnd);
            if len == 0 {
                return String::new();
            }
            let mut buf = vec![0u16; (len + 1) as usize];
            let copied = GetWindowTextW(hwnd, &mut buf);
            String::from_utf16_lossy(&buf[..copied as usize])
        }
    }

    fn get_window_pid(hwnd: HWND) -> u32 {
        let mut pid: u32 = 0;
        unsafe {
            GetWindowThreadProcessId(hwnd, Some(&mut pid));
        }
        pid
    }

    fn get_process_name(pid: u32) -> String {
        use windows::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        use windows::Win32::System::ProcessStatus::GetModuleFileNameExW;

        unsafe {
            let handle =
                OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok();
            if let Some(handle) = handle {
                let mut buf = vec![0u16; 260];
                let len = GetModuleFileNameExW(handle, None, &mut buf);
                let _ = windows::Win32::Foundation::CloseHandle(handle);
                if len > 0 {
                    let full = String::from_utf16_lossy(&buf[..len as usize]);
                    // Extract just the filename
                    return full
                        .rsplit('\\')
                        .next()
                        .unwrap_or(&full)
                        .to_string();
                }
            }
        }
        String::new()
    }

    /// Returns the foreground window (id, app_name, title), filtering out our own
    /// PID and tool/popup windows.
    pub fn get_foreground_window(
        own_pid: u32,
    ) -> Option<(NativeWindowId, String, String)> {
        unsafe {
            let hwnd = GetForegroundWindow();
            if hwnd.0.is_null() {
                return None;
            }

            // Skip our own windows
            let pid = get_window_pid(hwnd);
            if pid == own_pid {
                return None;
            }

            // Skip invisible / minimized
            if !IsWindowVisible(hwnd).as_bool() {
                return None;
            }
            if IsIconic(hwnd).as_bool() {
                return None;
            }

            // Skip tool windows and no-activate windows
            let ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
            if ex_style & WS_EX_TOOLWINDOW.0 != 0 {
                return None;
            }
            if ex_style & WS_EX_NOACTIVATE.0 != 0 {
                return None;
            }

            let title = get_window_text(hwnd);
            // Skip windows with no title (likely system/hidden)
            if title.is_empty() {
                return None;
            }

            let app_name = get_process_name(pid);

            Some((hwnd_to_id(hwnd), app_name, title))
        }
    }

    pub fn get_window_rect_platform(id: NativeWindowId) -> Option<WindowRect> {
        unsafe {
            let hwnd = id_to_hwnd(id);
            let mut rect = RECT::default();
            if GetWindowRect(hwnd, &mut rect).is_ok() {
                // Account for DPI scaling — GetWindowRect returns physical pixels
                // We work in physical pixels throughout to avoid rounding issues.
                Some(WindowRect {
                    x: rect.left,
                    y: rect.top,
                    width: (rect.right - rect.left).max(0) as u32,
                    height: (rect.bottom - rect.top).max(0) as u32,
                })
            } else {
                None
            }
        }
    }

    pub fn set_window_rect_platform(id: NativeWindowId, rect: WindowRect) -> bool {
        unsafe {
            let hwnd = id_to_hwnd(id);
            SetWindowPos(
                hwnd,
                HWND_TOP,
                rect.x,
                rect.y,
                rect.width as i32,
                rect.height as i32,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
            .is_ok()
        }
    }

    pub fn is_window_fullscreen_platform(id: NativeWindowId) -> bool {
        unsafe {
            let hwnd = id_to_hwnd(id);

            // Get the window rect
            let mut win_rect = RECT::default();
            if GetWindowRect(hwnd, &mut win_rect).is_err() {
                return false;
            }

            // Get the monitor that contains this window
            let monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            let mut monitor_info = windows::Win32::Graphics::Gdi::MONITORINFO {
                cbSize: std::mem::size_of::<windows::Win32::Graphics::Gdi::MONITORINFO>() as u32,
                ..Default::default()
            };
            if !windows::Win32::Graphics::Gdi::GetMonitorInfoW(monitor, &mut monitor_info)
                .as_bool()
            {
                return false;
            }

            let mon = monitor_info.rcMonitor;
            // Window is fullscreen if it covers the entire monitor
            win_rect.left <= mon.left
                && win_rect.top <= mon.top
                && win_rect.right >= mon.right
                && win_rect.bottom >= mon.bottom
        }
    }
}

// ===========================================================================
// Platform: macOS
// ===========================================================================

#[cfg(target_os = "macos")]
mod platform {
    use super::{NativeWindowId, WindowRect};
    use std::process::Command;

    pub fn get_foreground_window(
        own_pid: u32,
    ) -> Option<(NativeWindowId, String, String)> {
        // Get frontmost app info via AppleScript
        let output = Command::new("osascript")
            .args([
                "-e",
                r#"tell application "System Events"
                    set fp to first application process whose frontmost is true
                    set appName to name of fp
                    set appPID to unix id of fp
                    set winTitle to ""
                    try
                        set winTitle to title of first window of fp
                    end try
                    return appName & "|" & appPID & "|" & winTitle
                end tell"#,
            ])
            .output()
            .ok()?;

        let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let parts: Vec<&str> = raw.splitn(3, '|').collect();
        if parts.len() < 2 {
            return None;
        }

        let app_name = parts[0].to_string();
        let pid: u32 = parts[1].parse().unwrap_or(0);
        let window_title = parts.get(2).unwrap_or(&"").to_string();

        // Skip our own process
        if pid == own_pid {
            return None;
        }

        Some((NativeWindowId(pid as i64), app_name, window_title))
    }

    pub fn get_window_rect_platform(id: NativeWindowId) -> Option<WindowRect> {
        let pid = id.0;
        let script = format!(
            r#"tell application "System Events"
                set fp to first application process whose unix id is {}
                set pos to position of first window of fp
                set sz to size of first window of fp
                return (item 1 of pos) & "|" & (item 2 of pos) & "|" & (item 1 of sz) & "|" & (item 2 of sz)
            end tell"#,
            pid
        );

        let output = Command::new("osascript")
            .args(["-e", &script])
            .output()
            .ok()?;

        let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let parts: Vec<i32> = raw.split('|').filter_map(|s| s.parse().ok()).collect();
        if parts.len() != 4 {
            return None;
        }

        Some(WindowRect {
            x: parts[0],
            y: parts[1],
            width: parts[2].max(0) as u32,
            height: parts[3].max(0) as u32,
        })
    }

    pub fn set_window_rect_platform(id: NativeWindowId, rect: WindowRect) -> bool {
        let pid = id.0;
        let script = format!(
            r#"tell application "System Events"
                set fp to first application process whose unix id is {}
                set position of first window of fp to {{{}, {}}}
                set size of first window of fp to {{{}, {}}}
            end tell"#,
            pid, rect.x, rect.y, rect.width, rect.height
        );

        Command::new("osascript")
            .args(["-e", &script])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    pub fn is_window_fullscreen_platform(id: NativeWindowId) -> bool {
        let pid = id.0;
        let script = format!(
            r#"tell application "System Events"
                set fp to first application process whose unix id is {}
                try
                    return value of attribute "AXFullScreen" of first window of fp
                on error
                    return false
                end try
            end tell"#,
            pid
        );

        Command::new("osascript")
            .args(["-e", &script])
            .output()
            .map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .trim()
                    .eq_ignore_ascii_case("true")
            })
            .unwrap_or(false)
    }
}

// ===========================================================================
// Platform: Linux
// ===========================================================================

#[cfg(target_os = "linux")]
mod platform {
    use super::{NativeWindowId, WindowRect};
    use std::process::Command;

    pub fn get_foreground_window(
        own_pid: u32,
    ) -> Option<(NativeWindowId, String, String)> {
        // Get active window ID
        let output = Command::new("xdotool")
            .arg("getactivewindow")
            .output()
            .ok()?;

        if !output.status.success() {
            return None;
        }

        let wid_str = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let wid: i64 = wid_str.parse().ok()?;

        // Get PID
        let pid_output = Command::new("xdotool")
            .args(["getwindowpid", &wid_str])
            .output()
            .ok()?;

        let pid: u32 = String::from_utf8_lossy(&pid_output.stdout)
            .trim()
            .parse()
            .unwrap_or(0);

        if pid == own_pid {
            return None;
        }

        // Get window title
        let title_output = Command::new("xdotool")
            .args(["getwindowname", &wid_str])
            .output()
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();

        // Get app name from /proc
        let app_name = if pid > 0 {
            std::fs::read_to_string(format!("/proc/{}/comm", pid))
                .map(|s| s.trim().to_string())
                .unwrap_or_default()
        } else {
            String::new()
        };

        Some((NativeWindowId(wid), app_name, title_output))
    }

    pub fn get_window_rect_platform(id: NativeWindowId) -> Option<WindowRect> {
        let wid = id.0.to_string();

        // xdotool getwindowgeometry --shell returns X, Y, WIDTH, HEIGHT
        let output = Command::new("xdotool")
            .args(["getwindowgeometry", "--shell", &wid])
            .output()
            .ok()?;

        if !output.status.success() {
            return None;
        }

        let text = String::from_utf8_lossy(&output.stdout);
        let mut x = 0i32;
        let mut y = 0i32;
        let mut w = 0u32;
        let mut h = 0u32;

        for line in text.lines() {
            if let Some(val) = line.strip_prefix("X=") {
                x = val.parse().unwrap_or(0);
            } else if let Some(val) = line.strip_prefix("Y=") {
                y = val.parse().unwrap_or(0);
            } else if let Some(val) = line.strip_prefix("WIDTH=") {
                w = val.parse().unwrap_or(0);
            } else if let Some(val) = line.strip_prefix("HEIGHT=") {
                h = val.parse().unwrap_or(0);
            }
        }

        Some(WindowRect {
            x,
            y,
            width: w,
            height: h,
        })
    }

    pub fn set_window_rect_platform(id: NativeWindowId, rect: WindowRect) -> bool {
        let wid = id.0.to_string();

        // Move and resize using xdotool
        Command::new("xdotool")
            .args([
                "windowmove",
                "--sync",
                &wid,
                &rect.x.to_string(),
                &rect.y.to_string(),
            ])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
            && Command::new("xdotool")
                .args([
                    "windowsize",
                    "--sync",
                    &wid,
                    &rect.width.to_string(),
                    &rect.height.to_string(),
                ])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
    }

    pub fn is_window_fullscreen_platform(id: NativeWindowId) -> bool {
        let wid = id.0.to_string();

        // Check _NET_WM_STATE for fullscreen
        let output = Command::new("xprop")
            .args(["-id", &wid, "_NET_WM_STATE"])
            .output()
            .ok();

        output
            .map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .contains("_NET_WM_STATE_FULLSCREEN")
            })
            .unwrap_or(false)
    }
}

// ===========================================================================
// Platform: unsupported
// ===========================================================================

#[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
mod platform {
    use super::{NativeWindowId, WindowRect};

    pub fn get_foreground_window(
        _own_pid: u32,
    ) -> Option<(NativeWindowId, String, String)> {
        None
    }

    pub fn get_window_rect_platform(_id: NativeWindowId) -> Option<WindowRect> {
        None
    }

    pub fn set_window_rect_platform(_id: NativeWindowId, _rect: WindowRect) -> bool {
        false
    }

    pub fn is_window_fullscreen_platform(_id: NativeWindowId) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// Platform dispatch
// ---------------------------------------------------------------------------

fn get_foreground_window(own_pid: u32) -> Option<(NativeWindowId, String, String)> {
    platform::get_foreground_window(own_pid)
}

fn get_window_rect(id: NativeWindowId) -> Option<WindowRect> {
    platform::get_window_rect_platform(id)
}

fn set_window_rect(id: NativeWindowId, rect: WindowRect) -> bool {
    platform::set_window_rect_platform(id, rect)
}

fn is_window_fullscreen(id: NativeWindowId) -> bool {
    platform::is_window_fullscreen_platform(id)
}
