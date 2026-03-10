mod commands;
mod context;
mod fs_commands;
mod fs_thumbnail;
mod fs_watcher;
mod oauth;

#[cfg(desktop)]
mod quick_ask;
#[cfg(desktop)]
mod side_panel;
#[cfg(desktop)]
mod tray;
#[cfg(desktop)]
mod vibrancy;
#[cfg(desktop)]
mod window_attach;

use tauri::{Emitter, Manager};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Force X11 backend on Linux/Wayland so window positioning works.
    // Wayland does not allow apps to set their own window position.
    #[cfg(target_os = "linux")]
    {
        if std::env::var("WAYLAND_DISPLAY").is_ok() {
            std::env::set_var("GDK_BACKEND", "x11");
        }
    }
    #[allow(unused_mut)]
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init());

    // Desktop-only plugins
    #[cfg(desktop)]
    {
        builder = builder
            .plugin(tauri_plugin_global_shortcut::Builder::new().build())
            .plugin(tauri_plugin_positioner::init())
            .plugin(tauri_plugin_autostart::init(
                tauri_plugin_autostart::MacosLauncher::LaunchAgent,
                Some(vec!["--minimized"]),
            ));
    }

    builder = builder.manage(fs_watcher::WatcherState::default());

    #[cfg(desktop)]
    {
        builder = builder
            .manage(quick_ask::PendingQuickAsk(std::sync::Mutex::new(None)))
            .manage(side_panel::SidePanelState::default())
            .manage(window_attach::WindowAttachState::new())
            .manage(vibrancy::ActiveEffect(std::sync::Mutex::new(
                vibrancy::NativeEffect::None,
            )));
    }

    builder
        .invoke_handler(tauri::generate_handler![
            commands::read_access_token,
            commands::get_pocketpaw_config_dir,
            commands::check_backend_running,
            commands::check_pocketpaw_version,
            commands::check_pocketpaw_installed,
            commands::install_pocketpaw,
            commands::start_pocketpaw_backend,
            context::get_active_context,
            oauth::read_oauth_tokens,
            oauth::save_oauth_tokens,
            oauth::clear_oauth_tokens,
            oauth::proxy_post,
            oauth::proxy_get,
            fs_commands::fs_read_dir,
            fs_commands::fs_read_file_text,
            fs_commands::fs_write_file,
            fs_commands::fs_delete,
            fs_commands::fs_rename,
            fs_commands::fs_stat,
            fs_commands::fs_create_dir,
            fs_commands::fs_exists,
            fs_commands::fs_read_file_base64,
            fs_commands::fs_resolve_path,
            fs_commands::fs_parent_dir,
            fs_commands::fs_get_default_dirs,
            fs_commands::fs_copy_file,
            fs_commands::fs_copy_dir,
            fs_commands::fs_stat_extended,
            fs_commands::fs_open_in_terminal,
            fs_commands::fs_search_recursive,
            fs_commands::fs_read_file_head,
            fs_thumbnail::fs_thumbnail,
            fs_watcher::fs_watch,
            fs_watcher::fs_unwatch,
            #[cfg(desktop)]
            oauth::start_oauth_server,
            #[cfg(desktop)]
            side_panel::toggle_side_panel,
            #[cfg(desktop)]
            side_panel::show_side_panel,
            #[cfg(desktop)]
            side_panel::hide_side_panel,
            #[cfg(desktop)]
            side_panel::collapse_side_panel,
            #[cfg(desktop)]
            side_panel::expand_side_panel,
            #[cfg(desktop)]
            side_panel::is_side_panel_collapsed,
            #[cfg(desktop)]
            side_panel::dock_side_panel,
            #[cfg(desktop)]
            quick_ask::toggle_quick_ask,
            #[cfg(desktop)]
            quick_ask::show_quick_ask,
            #[cfg(desktop)]
            quick_ask::hide_quick_ask,
            #[cfg(desktop)]
            quick_ask::quickask_to_sidepanel,
            #[cfg(desktop)]
            quick_ask::get_pending_quickask,
            #[cfg(desktop)]
            vibrancy::get_native_effect,
            #[cfg(desktop)]
            vibrancy::set_vibrancy_theme,
            #[cfg(desktop)]
            window_attach::set_attach_mode,
            #[cfg(desktop)]
            window_attach::get_attach_mode,
            #[cfg(desktop)]
            window_attach::get_attach_info,
            #[cfg(desktop)]
            window_attach::detach_side_panel,
        ])
        .setup(|_app| {
            // Desktop-only: system tray + close-to-tray
            #[cfg(desktop)]
            {
                tray::setup_tray(_app.handle())?;

                let window = _app.get_webview_window("main").unwrap();

                // Open devtools in debug builds
                #[cfg(debug_assertions)]
                window.open_devtools();

                // Apply native vibrancy/mica/acrylic to all pre-created windows
                let effect = vibrancy::apply_native_effect(&window, None);
                *_app
                    .state::<vibrancy::ActiveEffect>()
                    .0
                    .lock()
                    .unwrap() = effect;
                let _ = window.emit("native-effect", effect);

                for label in ["sidepanel", "quickask"] {
                    if let Some(win) = _app.get_webview_window(label) {
                        vibrancy::apply_native_effect(&win, None);
                    }
                }

                // Start the window attach polling loop
                window_attach::start_poll_loop(_app.handle().clone());

                let window_clone = window.clone();
                window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = window_clone.hide();
                    }
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
