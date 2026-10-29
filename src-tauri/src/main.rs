use raw_window_handle::HasWindowHandle;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::{Emitter, Manager, State};

const ASSISTANT_W: f64 = 1152.0;
const ASSISTANT_H: f64 = 683.0;
const MAIN_W: f64 = 427.0;
const DEFAULT_GAP: f64 = 14.0;
const DEFAULT_OPACITY: f64 = 0.72;
const BACKEND_PORT: u16 = 8765;
/// Must match `bundle.identifier` in tauri.conf.json: Tauri resolves
/// `app_config_dir()` to `%APPDATA%\<bundle identifier>` on Windows.
const APP_DIR_NAME: &str = "com.jarvis.assistant";

#[derive(Serialize, Deserialize, Default, Clone)]
struct ModelSpec {
    #[serde(default)]
    provider: String,
    #[serde(default)]
    model: String,
}

#[derive(Serialize, Deserialize, Default, Clone)]
struct AppConfig {
    #[serde(default)]
    api_keys: HashMap<String, String>,
    #[serde(default)]
    selected_model: HashMap<String, String>,
    #[serde(default)]
    selected_primary: Option<ModelSpec>,
    #[serde(default)]
    workdir: Option<String>,
    // Code-agent tools the user switched off in the UI. Names only; the Python
    // server filters them out of the prompt and the tool schema per request.
    #[serde(default)]
    disabled_tools: Vec<String>,
}

struct AppState {
    gap: Mutex<f64>,
    opacity: Mutex<f64>,
    assistant_visible: Mutex<bool>,
    settings_visible: Mutex<bool>,
    /// Was the settings window open before the assistant took over the screen?
    /// Restoring the desktop has to bring back exactly what was there.
    settings_before_console: Mutex<bool>,
    config: Mutex<AppConfig>,
}

fn config_path(app: &tauri::AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());

    // Debug: Ensure directory exists
    if let Some(parent) = dir.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    dir.join("api_keys.json")
}

fn gap_path(app: &tauri::AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
    dir.join("gap.json")
}

fn opacity_path(app: &tauri::AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
    dir.join("opacity.json")
}

fn load_config(app: &tauri::AppHandle) -> AppConfig {
    let p = config_path(app);
    std::fs::read_to_string(&p)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_config(app: &tauri::AppHandle, config: &AppConfig) {
    let p = config_path(app);
    eprintln!("[debug] Saving config to: {}", p.display());
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    match serde_json::to_string_pretty(config) {
        Ok(s) => match std::fs::write(&p, s) {
            Ok(_) => eprintln!("[debug] Config saved successfully"),
            Err(e) => eprintln!("[debug] Error writing config: {}", e),
        },
        Err(e) => eprintln!("[debug] Error serializing config: {}", e),
    }
}

fn load_gap(app: &tauri::AppHandle) -> f64 {
    let p = gap_path(app);
    std::fs::read_to_string(&p)
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .unwrap_or(DEFAULT_GAP)
}

fn save_gap(app: &tauri::AppHandle, gap: f64) {
    let p = gap_path(app);
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&p, format!("{}", gap));
}

fn load_opacity(app: &tauri::AppHandle) -> f64 {
    let p = opacity_path(app);
    std::fs::read_to_string(&p)
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .unwrap_or(DEFAULT_OPACITY)
}

fn save_opacity(app: &tauri::AppHandle, opacity: f64) {
    let p = opacity_path(app);
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&p, format!("{}", opacity));
}

/// Per-user directory the Python backend writes to. Mirrors
/// `app_config_dir()` so both sides of the app agree on one location.
fn data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("JARVIS_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir);
        }
    }
    match std::env::var("APPDATA") {
        Ok(appdata) if !appdata.trim().is_empty() => PathBuf::from(appdata).join(APP_DIR_NAME),
        _ => std::env::current_dir()
            .unwrap_or_default()
            .join(".jarvis"),
    }
}

/// Repository root, but only while running from source (dev mode).
fn find_project_root() -> Option<PathBuf> {
    let cwd = std::env::current_dir().ok()?;
    let mut candidates = vec![cwd.clone()];
    if let Some(parent) = cwd.parent() {
        candidates.push(parent.to_path_buf());
    }
    candidates
        .into_iter()
        .find(|p| p.join("server").join("main.py").is_file())
}

fn documents_dir() -> PathBuf {
    std::env::var("USERPROFILE")
        .map(|home| PathBuf::from(home).join("Documents"))
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default())
}

/// Agent working folder. Dev runs inside the repo; an installed build must
/// never use the (read-only) install directory, so it falls back to
/// `Documents\Jarvis`.
fn default_workdir() -> PathBuf {
    find_project_root().unwrap_or_else(|| documents_dir().join("Jarvis"))
}

fn load_config_at(dir: &Path) -> AppConfig {
    std::fs::read_to_string(dir.join("api_keys.json"))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

struct PythonServer(Option<Child>);

impl Drop for PythonServer {
    fn drop(&mut self) {
        if let Some(ref mut child) = self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn port_is_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// PIDs of the processes listening on `port` (Windows only).
fn listening_pids(port: u16) -> Vec<u32> {
    if !cfg!(windows) {
        return Vec::new();
    }

    let output = match Command::new("netstat").args(["-ano", "-p", "TCP"]).output() {
        Ok(o) => o,
        Err(_) => return Vec::new(),
    };

    let needle = format!(":{}", port);
    let mut pids = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let cols: Vec<&str> = line.split_whitespace().collect();
        // proto | local | foreign | state | pid
        if cols.len() < 5 || !cols[3].eq_ignore_ascii_case("LISTENING") {
            continue;
        }
        if !cols[1].ends_with(&needle) {
            continue;
        }
        if let Ok(pid) = cols[4].parse::<u32>() {
            if pid != std::process::id() && !pids.contains(&pid) {
                pids.push(pid);
            }
        }
    }
    pids
}

fn process_image(pid: u32) -> String {
    Command::new("tasklist")
        .args(["/FI", &format!("PID eq {}", pid), "/NH", "/FO", "CSV"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_lowercase())
        .unwrap_or_default()
}

/// Kill a python backend left behind by a previous run.
///
/// Ctrl+C (or closing the console) terminates the Tauri process without running
/// its destructors, so the python child survives and keeps the port. Only
/// python processes are ever touched - if something else owns the port we only
/// warn about it.
fn stop_stale_backend(port: u16) {
    for pid in listening_pids(port) {
        let image = process_image(pid);
        if !image.contains("python") {
            eprintln!(
                "[backend] port {} is owned by pid {} ({}) - leaving it alone",
                port,
                pid,
                image.trim()
            );
            continue;
        }

        eprintln!("[backend] stopping stale python backend (pid {})", pid);
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .output();
    }
}

// ============================================================
// WINDOW SNAP ("РїСЂРёР»РёРїР°РЅРёРµ" Рє main-РѕРєРЅСѓ)
// ============================================================

/// Assistant window sticks to the LEFT of main, settings to the RIGHT.
/// Both keep the vertical position of main, so the three windows read as
/// one rigid row.
fn assistant_snap_x(main_x: f64, assistant_w: f64, gap: f64) -> f64 {
    (main_x - assistant_w - gap).round()
}

fn settings_snap_x(main_x: f64, main_w: f64, gap: f64) -> f64 {
    (main_x + main_w + gap).round()
}

/// Logical (DPI independent) width of a live window.
///
/// Snapping must follow the window we actually created, so a change in
/// tauri.conf.json can never leave a gap or an overlap behind.
fn window_width(app: &tauri::AppHandle, label: &str, fallback: f64) -> f64 {
    match app.get_webview_window(label) {
        Some(w) => match (w.outer_size(), w.scale_factor()) {
            (Ok(size), Ok(scale)) if scale > 0.0 => size.width as f64 / scale,
            _ => fallback,
        },
        None => fallback,
    }
}

/// Logical (DPI independent) height of a live window.
fn window_height(app: &tauri::AppHandle, label: &str, fallback: f64) -> f64 {
    match app.get_webview_window(label) {
        Some(w) => match (w.outer_size(), w.scale_factor()) {
            (Ok(size), Ok(scale)) if scale > 0.0 => size.height as f64 / scale,
            _ => fallback,
        },
        None => fallback,
    }
}

/// Top-left corner that centres a `w` x `h` window on the primary monitor.
fn center_on_primary_monitor(app: &tauri::AppHandle, w: f64, h: f64) -> Option<(i32, i32)> {
    let monitor = app.primary_monitor().ok().flatten()?;
    let scale = monitor.scale_factor();
    if scale <= 0.0 {
        return None;
    }

    let screen_w = monitor.size().width as f64 / scale;
    let screen_h = monitor.size().height as f64 / scale;
    let origin_x = monitor.position().x as f64 / scale;
    let origin_y = monitor.position().y as f64 / scale;

    let x = origin_x + (screen_w - w) / 2.0;
    let y = origin_y + (screen_h - h) / 2.0;
    Some((x.round() as i32, y.round() as i32))
}

fn start_python_backend(server_py: &Path, data_dir: &Path, workdir: &Path) -> Option<Child> {
    if port_is_open(BACKEND_PORT) {
        // A previous run that was killed with Ctrl+C leaves its python child
        // alive on the port; during development we take the port back instead
        // of silently attaching to an orphan.
        if cfg!(debug_assertions) {
            stop_stale_backend(BACKEND_PORT);
        }

        if port_is_open(BACKEND_PORT) {
            eprintln!(
                "[backend] 127.0.0.1:{} already in use, reusing existing server",
                BACKEND_PORT
            );
            return None;
        }
    }

    let candidates = ["python", "py"];
    for bin in candidates {
        let current_dir = server_py
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| PathBuf::from("."));
        let mut cmd = Command::new(bin);
        let child = cmd
            .arg(server_py)
            .current_dir(current_dir)
            .env("JARVIS_DATA_DIR", data_dir)
            .env("JARVIS_WORKDIR", workdir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .spawn();

        match child {
            Ok(child) => {
                eprintln!("[backend] started {} {}", bin, server_py.display());
                return Some(child);
            }
            Err(err) => {
                eprintln!("[backend] failed to start {}: {}", bin, err);
            }
        }
    }

    eprintln!("[backend] unable to start Python backend, UI will still open");
    None
}

#[tauri::command]
fn move_group(app: tauri::AppHandle, dx: f64, dy: f64) {
    let scale = app
        .get_webview_window("main")
        .and_then(|w| w.scale_factor().ok())
        .unwrap_or(1.0);
    let pdx = (dx * scale).round() as i32;
    let pdy = (dy * scale).round() as i32;

    let mut entries: Vec<(String, isize, i32, i32)> = Vec::new();
    for label in ["main", "assistant", "settings"] {
        if let Some(w) = app.get_webview_window(label) {
            if !w.is_visible().unwrap_or(false) {
                continue;
            }
            if let Ok(pos) = w.outer_position() {
                if let Ok(handle) = w.window_handle() {
                    if let raw_window_handle::RawWindowHandle::Win32(h) = handle.as_raw() {
                        let hwnd = h.hwnd.get();
                        entries.push((label.to_string(), hwnd as isize, pos.x + pdx, pos.y + pdy));
                    }
                }
            }
        }
    }

    if entries.is_empty() {
        return;
    }

    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            BeginDeferWindowPos, DeferWindowPos, EndDeferWindowPos, SWP_NOACTIVATE, SWP_NOSIZE,
            SWP_NOZORDER,
        };
        unsafe {
            let mut hdwp = BeginDeferWindowPos(entries.len() as i32);
            for (_, hwnd, x, y) in &entries {
                hdwp = DeferWindowPos(
                    hdwp,
                    *hwnd as *mut core::ffi::c_void,
                    std::ptr::null_mut(),
                    *x,
                    *y,
                    0,
                    0,
                    SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOZORDER,
                );
            }
            EndDeferWindowPos(hdwp);
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        for (label, _, x, y) in &entries {
            if let Some(w) = app.get_webview_window(label) {
                let _ = w.set_position(tauri::PhysicalPosition::new(*x, *y));
            }
        }
    }
}

#[tauri::command]
fn show_assistant(app: tauri::AppHandle, state: State<'_, AppState>) {
    *state.assistant_visible.lock().unwrap() = true;

    // Console mode: the assistant is the whole picture now, so the small
    // windows step aside instead of fighting it for screen space.
    let settings_was_open = *state.settings_visible.lock().unwrap();
    *state.settings_before_console.lock().unwrap() = settings_was_open;
    *state.settings_visible.lock().unwrap() = false;

    if let Some(main) = app.get_webview_window("main") {
        let _ = main.hide();
    }
    if let Some(settings) = app.get_webview_window("settings") {
        let _ = settings.hide();
    }

    if let Some(assistant) = app.get_webview_window("assistant") {
        let w = window_width(&app, "assistant", ASSISTANT_W);
        let h = window_height(&app, "assistant", ASSISTANT_H);
        if let Some((x, y)) = center_on_primary_monitor(&app, w, h) {
            let _ = assistant.set_position(tauri::LogicalPosition::new(x, y));
        }
        let _ = assistant.unminimize();
        let _ = assistant.show();
        let _ = assistant.set_focus();
    }

    let _ = app.emit("assistant-visibility", true);
}

/// Bring the desktop back: assistant away, main (and settings, if it was open)
/// back on screen and re-snapped to each other.
#[tauri::command]
fn restore_desktop(app: tauri::AppHandle, state: State<'_, AppState>) {
    *state.assistant_visible.lock().unwrap() = false;

    if let Some(assistant) = app.get_webview_window("assistant") {
        let _ = assistant.hide();
    }

    let settings_back = *state.settings_before_console.lock().unwrap();
    *state.settings_visible.lock().unwrap() = settings_back;

    if let Some(main) = app.get_webview_window("main") {
        let _ = main.show();
        let _ = main.set_focus();
    }

    if settings_back {
        if let (Some(main), Some(settings)) = (
            app.get_webview_window("main"),
            app.get_webview_window("settings"),
        ) {
            let gap = *state.gap.lock().unwrap();
            if let (Ok(pos), Ok(scale)) = (main.outer_position(), main.scale_factor()) {
                let main_x = (pos.x as f64) / scale;
                let main_y = (pos.y as f64) / scale;
                let main_w = window_width(&app, "main", MAIN_W);
                let x = settings_snap_x(main_x, main_w, gap) as i32;
                let _ = settings.set_position(tauri::LogicalPosition::new(x, main_y.round() as i32));
            }
            let _ = settings.show();
        }
    }

    let _ = app.emit("assistant-visibility", false);
}

#[tauri::command]
fn hide_assistant(app: tauri::AppHandle, state: State<'_, AppState>) {
    // Closing the assistant must never leave the user without a desktop.
    restore_desktop(app, state);
}

#[tauri::command]
fn show_settings(app: tauri::AppHandle, state: State<'_, AppState>) {
    *state.settings_visible.lock().unwrap() = true;
    if let (Some(main), Some(settings)) = (
        app.get_webview_window("main"),
        app.get_webview_window("settings"),
    ) {
        let gap = *state.gap.lock().unwrap();
        let scale = main.scale_factor().unwrap_or(1.0);
        let main_w = window_width(&app, "main", MAIN_W);
        if let Ok(pos) = main.outer_position() {
            let x = settings_snap_x((pos.x as f64) / scale, main_w, gap) as i32;
            let y = ((pos.y as f64) / scale).round() as i32;
            let _ = settings.set_position(tauri::LogicalPosition::new(x, y));
            let _ = settings.show();
            let _ = settings.set_focus();
        }
    }
}

/// Console mode safety net: if the assistant stops being on screen (minimised,
/// hidden, closed) while it is supposed to be the visible window, the desktop
/// comes back so the app can never end up with zero reachable windows.
fn spawn_console_watchdog(app: tauri::AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(700));

        let assistant_visible = app
            .try_state::<AppState>()
            .map(|s| *s.assistant_visible.lock().unwrap())
            .unwrap_or(false);
        if !assistant_visible {
            continue;
        }

        let on_screen = app
            .get_webview_window("assistant")
            .map(|w| {
                w.is_visible().unwrap_or(false) || w.is_minimized().unwrap_or(false)
            })
            .unwrap_or(false);

        if !on_screen {
            if let Some(state) = app.try_state::<AppState>() {
                restore_desktop(app.clone(), state);
            }
        }
    });
}

#[tauri::command]
fn hide_settings(app: tauri::AppHandle, state: State<'_, AppState>) {
    *state.settings_visible.lock().unwrap() = false;
    if let Some(settings) = app.get_webview_window("settings") {
        let _ = settings.hide();
    }
}

#[tauri::command]
fn get_gap(state: State<'_, AppState>) -> f64 {
    *state.gap.lock().unwrap()
}

#[tauri::command]
fn set_gap(app: tauri::AppHandle, state: State<'_, AppState>, gap: f64) {
    *state.gap.lock().unwrap() = gap;
    save_gap(&app, gap);
    if let Some(main) = app.get_webview_window("main") {
        let scale = main.scale_factor().unwrap_or(1.0);
        if let Ok(pos) = main.outer_position() {
            let main_x = (pos.x as f64) / scale;
            let main_y = (pos.y as f64) / scale;
            let main_w = window_width(&app, "main", MAIN_W);
            if let Some(assistant) = app.get_webview_window("assistant") {
                if assistant.is_visible().unwrap_or(false) {
                    let assistant_w = window_width(&app, "assistant", ASSISTANT_W);
                    let x = assistant_snap_x(main_x, assistant_w, gap) as i32;
                    let y = main_y.round() as i32;
                    let _ = assistant.set_position(tauri::LogicalPosition::new(x, y));
                }
            }
            if let Some(settings) = app.get_webview_window("settings") {
                if settings.is_visible().unwrap_or(false) {
                    let x = settings_snap_x(main_x, main_w, gap) as i32;
                    let y = main_y.round() as i32;
                    let _ = settings.set_position(tauri::LogicalPosition::new(x, y));
                }
            }
        }
    }
}

#[tauri::command]
fn get_opacity(state: State<'_, AppState>) -> f64 {
    *state.opacity.lock().unwrap()
}

#[tauri::command]
fn set_opacity(app: tauri::AppHandle, state: State<'_, AppState>, opacity: f64) {
    let opacity = opacity.clamp(0.1, 1.0);
    *state.opacity.lock().unwrap() = opacity;
    save_opacity(&app, opacity);
    let _ = app.emit("opacity-changed", opacity);
}

#[tauri::command]
fn get_config(state: State<'_, AppState>) -> AppConfig {
    state.config.lock().unwrap().clone()
}

#[tauri::command]
fn set_config(app: tauri::AppHandle, state: State<'_, AppState>, new_config: AppConfig) {
    let mut merged = new_config.clone();

    // The config file is shared with the Python server, which reads the API
    // keys from it. Never drop a key the file already has just because this
    // payload happened to omit it; an explicit empty string still removes it.
    let on_disk = load_config(&app);
    let mut keys = on_disk.api_keys.clone();
    for (k, v) in new_config.api_keys.iter() {
        keys.insert(k.clone(), v.clone());
    }
    keys.retain(|k, v| !k.trim().is_empty() && !v.trim().is_empty());
    merged.api_keys = keys;

    let mut config = state.config.lock().unwrap();
    *config = merged.clone();
    save_config(&app, &merged);
}

#[tauri::command]
fn get_workdir(app: tauri::AppHandle) -> String {
    let cfg = load_config(&app);
    if let Some(w) = cfg.workdir {
        return w;
    }
    default_workdir().display().to_string()
}

#[tauri::command]
fn set_workdir(path: String, app: tauri::AppHandle) -> Result<String, String> {
    let pbuf = std::path::PathBuf::from(&path);
    if !pbuf.exists() {
        return Err("Path does not exist".to_string());
    }
    if !pbuf.is_dir() {
        return Err("Path is not a directory".to_string());
    }
    let mut cfg = load_config(&app);
    cfg.workdir = Some(path.clone());
    save_config(&app, &cfg);
    Ok(path)
}

fn main() {
    let server_py = find_project_root().map(|p| p.join("server").join("main.py"));

    let data = data_dir();
    let _ = std::fs::create_dir_all(&data);
    let workdir = load_config_at(&data)
        .workdir
        .map(PathBuf::from)
        .unwrap_or_else(default_workdir);

    let python = match server_py {
        Some(ref server_py) => start_python_backend(server_py, &data, &workdir),
        None => {
            eprintln!("[backend] server/main.py not found, UI will still open");
            None
        }
    };

    let _guard = PythonServer(python);

    tauri::Builder::default()
        .setup(|app| {
            let config = load_config(app.handle());
            let gap = load_gap(app.handle());
            let opacity = load_opacity(app.handle());

            app.manage(AppState {
                gap: Mutex::new(gap),
                opacity: Mutex::new(opacity),
                assistant_visible: Mutex::new(false),
                settings_visible: Mutex::new(false),
                settings_before_console: Mutex::new(false),
                config: Mutex::new(config),
            });

            let _js = format!(
                "document.querySelectorAll('[data-panel-bg]').forEach(function(e){{e.style.background='rgba(1, 4, 8, {})}});",
                opacity
            );
            
            for label in ["main", "assistant", "settings"] {
                if let Some(w) = app.get_webview_window(label) {
                    let _ = w.eval(&_js);
                }
            }

            // Console mode puts the assistant alone on screen, so closing or
            // minimising it would strand the user with nothing to click.
            // Both actions hand control back to the desktop instead.
            if let Some(assistant) = app.get_webview_window("assistant") {
                let handle = app.handle().clone();
                let watched = assistant.clone();
                assistant.on_window_event(move |event| {
                    match event {
                        tauri::WindowEvent::CloseRequested { api, .. } => {
                            api.prevent_close();
                            let state = handle.state::<AppState>();
                            restore_desktop(handle.clone(), state);
                        }
                        tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Moved(_) => {}
                        _ => {}
                    }
                    let _ = &watched;
                });
            }

            spawn_console_watchdog(app.handle().clone());

            Ok(())
        })
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(tauri::generate_handler![
            move_group,
            show_assistant,
            hide_assistant,
            restore_desktop,
            show_settings,
            hide_settings,
            get_gap,
            set_gap,
            get_opacity,
            set_opacity,
            get_config,
            set_config,
            get_workdir,
            set_workdir,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn assistant_snaps_flush_to_the_left_of_main() {
        // main at x=800, assistant 1180 wide, gap 14 -> its right edge is 786
        let x = assistant_snap_x(800.0, ASSISTANT_W, 14.0);
        assert_eq!(x + ASSISTANT_W, 786.0);
    }

    #[test]
    fn settings_snaps_flush_to_the_right_of_main() {
        let x = settings_snap_x(800.0, MAIN_W, 14.0);
        assert_eq!(x, 800.0 + MAIN_W + 14.0);
    }

    #[test]
    fn gap_zero_closes_both_seams() {
        assert_eq!(assistant_snap_x(800.0, ASSISTANT_W, 0.0) + ASSISTANT_W, 800.0);
        assert_eq!(settings_snap_x(800.0, MAIN_W, 0.0), 800.0 + MAIN_W);
    }

    #[test]
    fn snap_uses_the_real_window_width_not_a_stale_constant() {
        // whatever the window really is (say 900), the seam stays exact
        let real_w = 900.0;
        let x = assistant_snap_x(1500.0, real_w, 14.0);
        assert_eq!(x + real_w, 1486.0);
    }

    #[test]
    fn selected_models_survive_a_save_load_cycle() {
        let cfg = AppConfig {
            api_keys: HashMap::from([("google".to_string(), "g".to_string())]),
            selected_model: HashMap::new(),
            selected_primary: Some(ModelSpec {
                provider: "google".to_string(),
                model: "gemini-3.1-flash-lite".to_string(),
            }),
            workdir: None,
            disabled_tools: Vec::new(),
        };

        let on_disk = serde_json::to_string(&cfg).unwrap();
        let back: AppConfig = serde_json::from_str(&on_disk).unwrap();

        let p = back.selected_primary.expect("primary model was dropped");
        assert_eq!(p.provider, "google");
        assert_eq!(p.model, "gemini-3.1-flash-lite");
    }

    #[test]
    fn null_selection_is_kept_as_absent() {
        let cfg = AppConfig {
            api_keys: HashMap::new(),
            selected_model: HashMap::new(),
            selected_primary: None,
            workdir: None,
            disabled_tools: Vec::new(),
        };
        let on_disk = serde_json::to_string(&cfg).unwrap();
        assert!(on_disk.contains("\"selected_primary\":null"));
        let back: AppConfig = serde_json::from_str(&on_disk).unwrap();
        assert!(back.selected_primary.is_none());
    }

    #[test]
    fn legacy_file_without_selection_still_loads() {
        let legacy = r#"{"api_keys":{"google":"g"},"selected_model":{"code":"gemini-1.5-flash"}}"#;
        let cfg: AppConfig = serde_json::from_str(legacy).unwrap();
        assert_eq!(cfg.api_keys.get("google").map(String::as_str), Some("g"));
        assert!(cfg.selected_primary.is_none());
    }
}
