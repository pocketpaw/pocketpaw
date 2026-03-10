use std::sync::Mutex;
use tauri::{AppHandle, Manager};
use tauri_plugin_positioner::{Position, WindowExt};

use crate::window_attach::{self, AttachMode};

/// Managed state for the side panel's collapse/dock modes.
pub struct SidePanelState {
    pub collapsed: Mutex<bool>,
    pub expanded_width: Mutex<f64>,
    pub docked_edge: Mutex<Option<String>>,
}

impl Default for SidePanelState {
    fn default() -> Self {
        Self {
            collapsed: Mutex::new(false),
            expanded_width: Mutex::new(340.0),
            docked_edge: Mutex::new(None),
        }
    }
}

/// Create or toggle the side panel window.
#[tauri::command]
pub fn toggle_side_panel(app: AppHandle) -> Result<(), String> {
    if let Some(panel) = app.get_webview_window("sidepanel") {
        let visible = panel.is_visible().unwrap_or(false);
        if visible {
            // Restore attached window before hiding
            window_attach::restore_and_detach(&app);
            panel.hide().map_err(|e| e.to_string())?;
        } else {
            position_side_panel(&app, &panel)?;
            panel.show().map_err(|e| e.to_string())?;
            panel.set_focus().map_err(|e| e.to_string())?;
            // If in Auto mode, try to attach to the foreground window
            window_attach::try_attach_to_foreground(&app);
        }
    }
    Ok(())
}

/// Show the side panel (create if needed).
#[tauri::command]
pub fn show_side_panel(app: AppHandle) -> Result<(), String> {
    if let Some(panel) = app.get_webview_window("sidepanel") {
        position_side_panel(&app, &panel)?;
        panel.show().map_err(|e| e.to_string())?;
        panel.set_focus().map_err(|e| e.to_string())?;
        // If in Auto mode, try to attach to the foreground window
        window_attach::try_attach_to_foreground(&app);
    }
    Ok(())
}

/// Hide the side panel.
#[tauri::command]
pub fn hide_side_panel(app: AppHandle) -> Result<(), String> {
    // Restore attached window before hiding
    window_attach::restore_and_detach(&app);
    if let Some(panel) = app.get_webview_window("sidepanel") {
        panel.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Collapse the side panel to a thin strip.
#[tauri::command]
pub fn collapse_side_panel(app: AppHandle) -> Result<(), String> {
    let state = app.state::<SidePanelState>();

    if let Some(panel) = app.get_webview_window("sidepanel") {
        // Save current width before collapsing
        if let Ok(size) = panel.inner_size() {
            let mut w = state.expanded_width.lock().unwrap();
            *w = size.width as f64;
        }

        panel
            .set_size(tauri::Size::Logical(tauri::LogicalSize {
                width: 48.0,
                height: 600.0,
            }))
            .map_err(|e| e.to_string())?;

        let mut collapsed = state.collapsed.lock().unwrap();
        *collapsed = true;
    }

    Ok(())
}

/// Expand the side panel back to its previous width.
#[tauri::command]
pub fn expand_side_panel(app: AppHandle) -> Result<(), String> {
    let state = app.state::<SidePanelState>();

    if let Some(panel) = app.get_webview_window("sidepanel") {
        let w = *state.expanded_width.lock().unwrap();
        panel
            .set_size(tauri::Size::Logical(tauri::LogicalSize {
                width: w,
                height: 600.0,
            }))
            .map_err(|e| e.to_string())?;

        let mut collapsed = state.collapsed.lock().unwrap();
        *collapsed = false;
    }

    Ok(())
}

/// Check if the side panel is collapsed.
#[tauri::command]
pub fn is_side_panel_collapsed(app: AppHandle) -> bool {
    let state = app.state::<SidePanelState>();
    let collapsed = *state.collapsed.lock().unwrap();
    collapsed
}

/// Dock the side panel to a screen edge.
#[tauri::command]
pub fn dock_side_panel(app: AppHandle, edge: String) -> Result<(), String> {
    let state = app.state::<SidePanelState>();

    if let Some(panel) = app.get_webview_window("sidepanel") {
        let monitor = panel
            .current_monitor()
            .map_err(|e| e.to_string())?
            .ok_or_else(|| "No monitor found".to_string())?;

        let screen_size = monitor.size();
        let screen_pos = monitor.position();
        let scale = monitor.scale_factor();

        let screen_w = screen_size.width as f64 / scale;
        let screen_h = screen_size.height as f64 / scale;
        let origin_x = screen_pos.x as f64 / scale;
        let origin_y = screen_pos.y as f64 / scale;

        let panel_width = if *state.collapsed.lock().unwrap() {
            48.0
        } else {
            *state.expanded_width.lock().unwrap()
        };

        let x = match edge.as_str() {
            "left" => origin_x,
            _ => origin_x + screen_w - panel_width, // default: right
        };

        panel
            .set_position(tauri::Position::Logical(tauri::LogicalPosition {
                x,
                y: origin_y,
            }))
            .map_err(|e| e.to_string())?;

        panel
            .set_size(tauri::Size::Logical(tauri::LogicalSize {
                width: panel_width,
                height: screen_h,
            }))
            .map_err(|e| e.to_string())?;

        let mut docked = state.docked_edge.lock().unwrap();
        *docked = Some(edge);
    }

    Ok(())
}

/// Position the side panel at the right edge of the screen, full height.
/// In Auto attach mode, positioning is handled by the attach system instead.
fn position_side_panel(
    app: &AppHandle,
    panel: &tauri::WebviewWindow,
) -> Result<(), String> {
    // If Auto mode is active, the attach system will position the panel
    let attach_state = app.state::<window_attach::WindowAttachState>();
    if *attach_state.mode.lock().unwrap() == AttachMode::Auto {
        // Let the attach system handle positioning after show
        return Ok(());
    }

    let state = app.state::<SidePanelState>();
    let panel_w = if *state.collapsed.lock().unwrap() {
        48.0
    } else {
        *state.expanded_width.lock().unwrap()
    };

    if let Ok(Some(monitor)) = panel.current_monitor() {
        let scale = monitor.scale_factor();
        let screen_w = monitor.size().width as f64 / scale;
        let screen_h = monitor.size().height as f64 / scale;
        let origin_x = monitor.position().x as f64 / scale;
        let origin_y = monitor.position().y as f64 / scale;

        let _ = panel.set_size(tauri::Size::Logical(tauri::LogicalSize {
            width: panel_w,
            height: screen_h,
        }));
        let _ = panel.set_position(tauri::Position::Logical(tauri::LogicalPosition {
            x: origin_x + screen_w - panel_w,
            y: origin_y,
        }));
    } else {
        let _ = panel.as_ref().window().move_window(Position::RightCenter);
    }

    Ok(())
}
