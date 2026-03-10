use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, WebviewWindow};

/// Which native effect is currently active.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum NativeEffect {
    Vibrancy,
    Mica,
    Acrylic,
    None,
}

/// Managed state so the frontend can query the active effect.
pub struct ActiveEffect(pub Mutex<NativeEffect>);

/// Apply the best available native transparency effect for the current platform.
/// Returns which effect was successfully applied.
pub fn apply_native_effect(_window: &WebviewWindow, dark: Option<bool>) -> NativeEffect {
    #[cfg(target_os = "macos")]
    {
        use window_vibrancy::{
            apply_vibrancy, NSVisualEffectMaterial, NSVisualEffectState,
        };
        if apply_vibrancy(
            _window,
            NSVisualEffectMaterial::Sidebar,
            Some(NSVisualEffectState::FollowsWindowActiveState),
            Some(10.0),
        )
        .is_ok()
        {
            return NativeEffect::Vibrancy;
        }
    }

    #[cfg(target_os = "windows")]
    {
        use window_vibrancy::{apply_acrylic, apply_mica};
        // Try Mica first (Windows 11)
        if apply_mica(_window, dark).is_ok() {
            return NativeEffect::Mica;
        }
        // Fallback to Acrylic (Windows 10 v1809+)
        let tint = if dark.unwrap_or(true) {
            Some((30, 30, 30, 80))
        } else {
            Some((240, 240, 240, 80))
        };
        if apply_acrylic(_window, tint).is_ok() {
            return NativeEffect::Acrylic;
        }
    }

    // Suppress unused variable warning on non-Windows platforms
    let _ = dark;

    NativeEffect::None
}

/// Clear any native effect from a window (best-effort).
#[allow(unused_variables)]
pub fn clear_native_effect(window: &WebviewWindow) {
    #[cfg(target_os = "macos")]
    {
        let _ = window_vibrancy::clear_vibrancy(window);
    }
    #[cfg(target_os = "windows")]
    {
        let _ = window_vibrancy::clear_mica(window);
        let _ = window_vibrancy::clear_acrylic(window);
    }
}

/// Tauri command: return the current native effect.
#[tauri::command]
pub fn get_native_effect(app: AppHandle) -> NativeEffect {
    let state = app.state::<ActiveEffect>();
    let effect = *state.0.lock().unwrap();
    effect
}

/// Tauri command: re-apply effects on all windows for a dark/light mode change.
/// On macOS vibrancy follows the system automatically, but on Windows Mica/Acrylic
/// needs to be re-applied with the new dark param.
#[tauri::command]
pub fn set_vibrancy_theme(app: AppHandle, dark: bool) -> NativeEffect {
    let mut result = NativeEffect::None;

    for label in ["main", "sidepanel", "quickask"] {
        if let Some(win) = app.get_webview_window(label) {
            clear_native_effect(&win);
            let effect = apply_native_effect(&win, Some(dark));
            if label == "main" {
                result = effect;
            }
        }
    }

    // Update managed state
    let state = app.state::<ActiveEffect>();
    *state.0.lock().unwrap() = result;

    result
}
