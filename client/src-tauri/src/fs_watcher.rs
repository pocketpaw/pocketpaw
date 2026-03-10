use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager};

#[derive(Debug, Serialize, Clone)]
pub struct FileChangeEvent {
    pub path: String,
    pub kind: String,
    pub is_dir: bool,
}

pub struct WatcherState {
    watcher: Mutex<Option<RecommendedWatcher>>,
    watched_path: Mutex<Option<String>>,
}

impl Default for WatcherState {
    fn default() -> Self {
        Self {
            watcher: Mutex::new(None),
            watched_path: Mutex::new(None),
        }
    }
}

fn event_kind_to_string(kind: &EventKind) -> Option<&'static str> {
    match kind {
        EventKind::Create(_) => Some("create"),
        EventKind::Modify(_) => Some("modify"),
        EventKind::Remove(_) => Some("delete"),
        _ => None,
    }
}

#[tauri::command]
pub fn fs_watch(path: String, app: AppHandle) -> Result<(), String> {
    let state = app.state::<WatcherState>();

    // Stop existing watcher
    {
        let mut w = state.watcher.lock().map_err(|e| e.to_string())?;
        *w = None;
        let mut wp = state.watched_path.lock().map_err(|e| e.to_string())?;
        *wp = None;
    }

    let app_handle = app.clone();
    let mut watcher = notify::recommended_watcher(move |res: Result<Event, notify::Error>| {
        if let Ok(event) = res {
            if let Some(kind_str) = event_kind_to_string(&event.kind) {
                for path in &event.paths {
                    let is_dir = std::fs::metadata(path)
                        .map(|m| m.is_dir())
                        .unwrap_or(false);
                    let payload = FileChangeEvent {
                        path: path.to_string_lossy().to_string(),
                        kind: kind_str.to_string(),
                        is_dir,
                    };
                    let _ = app_handle.emit("fs-change", &payload);
                }
            }
        }
    })
    .map_err(|e| format!("Failed to create watcher: {}", e))?;

    watcher
        .watch(path.as_ref(), RecursiveMode::NonRecursive)
        .map_err(|e| format!("Failed to watch path: {}", e))?;

    {
        let mut w = state.watcher.lock().map_err(|e| e.to_string())?;
        *w = Some(watcher);
        let mut wp = state.watched_path.lock().map_err(|e| e.to_string())?;
        *wp = Some(path);
    }

    Ok(())
}

#[tauri::command]
pub fn fs_unwatch(app: AppHandle) -> Result<(), String> {
    let state = app.state::<WatcherState>();
    let mut w = state.watcher.lock().map_err(|e| e.to_string())?;
    *w = None;
    let mut wp = state.watched_path.lock().map_err(|e| e.to_string())?;
    *wp = None;
    Ok(())
}
