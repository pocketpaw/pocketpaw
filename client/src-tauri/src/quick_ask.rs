use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager};

/// Stores a pending message from QuickAsk for the side panel to pick up on mount.
pub struct PendingQuickAsk(pub Mutex<Option<String>>);

/// Toggle the quick ask overlay window (show/hide).
#[tauri::command]
pub fn toggle_quick_ask(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("quickask") {
        let visible = win.is_visible().unwrap_or(false);
        if visible {
            win.hide().map_err(|e| e.to_string())?;
        } else {
            win.center().map_err(|e| e.to_string())?;
            win.show().map_err(|e| e.to_string())?;
            win.set_focus().map_err(|e| e.to_string())?;
            let _ = win.emit("quickask-shown", ());
        }
    }
    Ok(())
}

/// Show the quick ask window.
#[tauri::command]
pub fn show_quick_ask(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("quickask") {
        win.center().map_err(|e| e.to_string())?;
        win.show().map_err(|e| e.to_string())?;
        win.set_focus().map_err(|e| e.to_string())?;
        let _ = win.emit("quickask-shown", ());
    }
    Ok(())
}

/// Hide the quick ask window.
#[tauri::command]
pub fn hide_quick_ask(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("quickask") {
        win.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Send a QuickAsk message to the side panel: hides QuickAsk, opens the side panel,
/// and delivers the message.
#[tauri::command]
pub fn quickask_to_sidepanel(app: AppHandle, message: String) -> Result<(), String> {
    // Hide QuickAsk
    if let Some(win) = app.get_webview_window("quickask") {
        let _ = win.hide();
    }

    // Store the message for the side panel to pick up
    let state = app.state::<PendingQuickAsk>();
    *state.0.lock().unwrap() = Some(message.clone());

    if let Some(panel) = app.get_webview_window("sidepanel") {
        panel.show().map_err(|e| e.to_string())?;
        panel.set_focus().map_err(|e| e.to_string())?;
        let _ = panel.emit("quickask-message", message);
    }

    Ok(())
}

/// Returns and clears any pending QuickAsk message (called by the side panel on mount).
#[tauri::command]
pub fn get_pending_quickask(app: AppHandle) -> Option<String> {
    let state = app.state::<PendingQuickAsk>();
    let result = state.0.lock().unwrap().take();
    result
}
