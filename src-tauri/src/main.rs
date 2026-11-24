use raw_window_handle::HasWindowHandle;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::webview::{PermissionKind, PermissionResponse};
use tauri::{Emitter, Manager, State};
use sha2::{Digest, Sha256};

const ASSISTANT_W: f64 = 1152.0;
const ASSISTANT_H: f64 = 683.0;
const MAIN_W: f64 = 427.0;
const DEFAULT_GAP: f64 = 14.0;
const DEFAULT_OPACITY: f64 = 0.72;
/// Built-in "current" chat/voice palette. Any saved value other than `onyx`
/// falls back to this one so a stale/foreign file can never break the UI.
const DEFAULT_THEME: &str = "aether";
const THEME_ONYX: &str = "onyx";
const BACKEND_PORT: u16 = 8765;
/// Must match `bundle.identifier` in tauri.conf.json: Tauri resolves
/// `app_config_dir()` to `%APPDATA%\<bundle identifier>` on Windows.
const APP_DIR_NAME: &str = "com.jarvis.assistant";
/// Token TTL in seconds (24 hours)
const TOKEN_TTL_SECONDS: u64 = 24 * 60 * 60;

/// Lock a UI-state mutex without panicking the whole app.
///
/// `.unwrap()` here killed the Tauri process if another command panicked while
/// holding the lock (poisoned state). These guards only hold copies of plain
/// settings, so recovering the inner value is safe and strictly better than
/// losing the whole window.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| {
        eprintln!("[state] recovered a poisoned mutex; using its last value");
        poisoned.into_inner()
    })
}

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
    // Audio settings are read by the Python backend from the same config file.
    // Keep them as raw JSON so a frontend save can never erase settings written
    // by /settings/stt or /settings/tts.
    #[serde(default)]
    stt: Option<serde_json::Value>,
    #[serde(default)]
    tts: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Default, Clone)]
struct BackendStatus {
    /// `/health` answered 200 - the UI can talk to the API.
    ready: bool,
    /// Startup failed or an established backend stopped answering.
    failed: bool,
    /// Human readable reason, shown in the UI when `failed` is true.
    message: String,
    /// "dev" (python from PATH) or "bundled" (interpreter from $RESOURCE).
    mode: String,
}

struct AppState {
    gap: Mutex<f64>,
    opacity: Mutex<f64>,
    theme: Mutex<String>,
    assistant_visible: Mutex<bool>,
    settings_visible: Mutex<bool>,
    /// Was the settings window open before the assistant took over the screen?
    /// Restoring the desktop has to bring back exactly what was there.
    settings_before_console: Mutex<bool>,
    config: Mutex<AppConfig>,
    backend: Mutex<BackendStatus>,
    /// Per-launch secret the UI must send to the local API. Never written to
    /// disk and never logged; a new value on every start.
    api_token: String,
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

fn theme_path(app: &tauri::AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_config_dir()
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
    dir.join("theme.json")
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

fn normalize_theme(raw: &str) -> String {
    match raw.trim() {
        THEME_ONYX => THEME_ONYX.to_string(),
        _ => DEFAULT_THEME.to_string(),
    }
}

fn load_theme(app: &tauri::AppHandle) -> String {
    let p = theme_path(app);
    std::fs::read_to_string(&p)
        .ok()
        .map(|s| normalize_theme(&s))
        .unwrap_or_else(|| DEFAULT_THEME.to_string())
}

fn save_theme(app: &tauri::AppHandle, theme: &str) {
    let p = theme_path(app);
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&p, theme);
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

/// Token storage file path (hash + expiry)
fn token_file_path() -> PathBuf {
    data_dir().join("api_token.json")
}

/// Load stored token hash and expiry from disk
fn load_token_info() -> Option<(String, u64)> {
    let path = token_file_path();
    if !path.is_file() {
        return None;
    }
    let content = std::fs::read_to_string(path).ok()?;
    let value: serde_json::Value = serde_json::from_str(&content).ok()?;
    let hash = value.get("hash")?.as_str()?.to_string();
    let expiry = value.get("expiry")?.as_u64()?;
    Some((hash, expiry))
}

/// Save token hash and expiry to disk atomically
fn save_token_info(token: &str, expiry: u64) -> Result<(), String> {
    // Hash token with SHA256 to match Python's hashlib.sha256
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    let hash = format!("{:x}", hasher.finalize());

    let value = serde_json::json!({
        "hash": hash,
        "expiry": expiry
    });

    let path = token_file_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let content = serde_json::to_string_pretty(&value).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, content).map_err(|e| e.to_string())?;
    std::fs::rename(tmp, path).map_err(|e| e.to_string())?;
    Ok(())
}

/// Check if stored token is expired
fn is_token_expired() -> bool {
    if let Some((_, expiry)) = load_token_info() {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        return now > expiry;
    }
    false
}

/// Generate a new token with expiry and save to disk
fn generate_and_save_token() -> (String, u64) {
    let token = generate_api_token();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let expiry = now + TOKEN_TTL_SECONDS;
    let _ = save_token_info(&token, expiry);
    (token, expiry)
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

fn load_config_at(dir: &Path) -> AppConfig {
    std::fs::read_to_string(dir.join("api_keys.json"))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

/// Owns the backend process. Dropping it (app exit) stops the server.
struct PythonServer(Mutex<Option<Child>>);

impl Drop for PythonServer {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(ref mut child) = *guard {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

/// One way of launching the backend: what to run, with which entry point.
struct BackendCommand {
    program: PathBuf,
    server_py: PathBuf,
    cwd: PathBuf,
    mode: &'static str,
}

/// Resolve the backend for this build.
///
/// Dev: `python` (or the `py` launcher) from PATH with `server/main.py` in the
/// checkout. Packaged: the embeddable interpreter and the copied sources under
/// `$RESOURCE\backend` - the installer ships both, nothing is installed system
/// wide and the user does not need Python.
fn resolve_backend(app: &tauri::AppHandle) -> Result<Vec<BackendCommand>, String> {
    if let Some(root) = find_project_root() {
        let server_py = root.join("server").join("main.py");
        let cwd = root.join("server");
        return Ok(vec![
            BackendCommand {
                program: PathBuf::from("python"),
                server_py: server_py.clone(),
                cwd: cwd.clone(),
                mode: "dev",
            },
            BackendCommand {
                program: PathBuf::from("py"),
                server_py,
                cwd,
                mode: "dev",
            },
        ]);
    }

    let base = app
        .path()
        .resource_dir()
        .map_err(|e| format!("Cannot resolve the app resource directory: {}", e))?
        .join("backend");
    let program = base.join("python").join("python.exe");
    let server_py = base.join("app").join("server").join("main.py");

    if !program.is_file() {
        return Err(format!(
            "Bundled Python runtime is missing ({}). Reinstall Jarvis, or check that antivirus did not quarantine it.",
            program.display()
        ));
    }
    if !server_py.is_file() {
        return Err(format!(
            "Backend sources are missing ({}). The installation looks incomplete; reinstall Jarvis.",
            server_py.display()
        ));
    }

    Ok(vec![BackendCommand {
        program,
        server_py,
        cwd: base.join("app"),
        mode: "bundled",
    }])
}

/// GET /health over a raw socket: the backend is up only when it answers 200
/// *and* identifies itself. Another program squatting on port 8765 must never
/// be mistaken for our backend, or the UI would talk to a stranger.
fn backend_healthy() -> bool {
    let addr = match format!("127.0.0.1:{}", BACKEND_PORT).parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(700)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    if stream
        .write_all(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }

    let mut buf = Vec::new();
    let mut chunk = [0u8; 1024];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > 8192 {
                    break;
                }
            }
            Err(_) => break,
        }
    }

    let response = String::from_utf8_lossy(&buf);
    let status_ok =
        response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200");
    if !status_ok {
        return false;
    }
    // `{"status":"ok","service":"jarvis-backend",...}`
    response.contains("\"service\":\"jarvis-backend\"")
}

fn backend_log_path(data_dir: &Path) -> PathBuf {
    data_dir.join("backend.log")
}

/// Keep the UI informed about the backend without blocking startup.
///
/// `status` describes the state right after launching:
/// * `failed`  - nothing was started (missing runtime, port taken); report once;
/// * `ready`   - an already healthy backend was reused; watch it from now on;
/// * otherwise - wait for `/health` to answer, then keep watching.
fn spawn_backend_watchdog(app: tauri::AppHandle, mut status: BackendStatus) {
    std::thread::spawn(move || {
        if status.failed {
            push_backend_status(&app, status);
            return;
        }

        let mut announced_healthy = status.ready;
        let deadline = Instant::now() + Duration::from_secs(60);

        loop {
            let healthy = backend_healthy();

            if healthy && !status.ready {
                status.ready = true;
                status.failed = false;
                status.message.clear();
                announced_healthy = true;
                push_backend_status(&app, status.clone());
            } else if !healthy && announced_healthy && !status.failed {
                status.ready = false;
                status.failed = true;
                status.message = "The local backend stopped responding. Close and reopen Jarvis; \
                     if it keeps happening, see backend.log in the app data folder."
                    .to_string();
                push_backend_status(&app, status.clone());
            } else if !healthy && !announced_healthy && Instant::now() >= deadline && !status.failed {
                status.failed = true;
                status.message = "The local backend did not start within 60 seconds. \
                     Antivirus software sometimes blocks backend\\python\\python.exe; \
                     see backend.log in the app data folder."
                    .to_string();
                push_backend_status(&app, status.clone());
            }

            std::thread::sleep(Duration::from_millis(if announced_healthy { 5000 } else { 500 }));
        }
    });
}

fn push_backend_status(app: &tauri::AppHandle, status: BackendStatus) {
    if let Some(state) = app.try_state::<AppState>() {
        if let Ok(mut guard) = state.backend.lock() {
            *guard = status.clone();
        }
    }
    let _ = app.emit("backend-status", status);
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

/// Image name and command line of `pid`, lowercased, one per line.
///
/// `tasklist` only reports the image name, and `python.exe` alone does not prove
/// that the process on our port is *our* backend: any other Python program may
/// hold it. Only a process that runs `server/main.py` may be killed.
fn process_details(pid: u32) -> String {
    if !cfg!(windows) {
        return String::new();
    }

    let script = format!(
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId={}\" -ErrorAction SilentlyContinue; \
         if ($p) {{ Write-Output $p.Name; Write-Output $p.CommandLine }}",
        pid
    );
    Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_lowercase())
        .unwrap_or_default()
}

/// Does this image name + command line belong to *this* Jarvis backend?
///
/// Checking for a generic `server/main.py` was too loose: any unrelated Python
/// program with that filename on its command line could be killed. The caller
/// passes the exact `server_py` paths it is about to launch, so only a process
/// running one of those paths is considered ours.
fn is_jarvis_backend(details: &str, expected: &[PathBuf]) -> bool {
    if !details.contains("python") {
        return false;
    }
    let command_line = details.replace('/', "\\");
    expected.iter().any(|path| {
        let needle = path.to_string_lossy().to_lowercase().replace('/', "\\");
        !needle.is_empty() && command_line.contains(&needle)
    })
}

/// Kill a python backend left behind by a previous run.
///
/// Ctrl+C (or closing the console) terminates the Tauri process without running
/// its destructors, so the python child survives and keeps the port. Only a
/// process that is recognisably our backend is ever touched - if something else
/// owns the port we only warn about it.
fn stop_stale_backend(port: u16, expected: &[PathBuf]) {
    for pid in listening_pids(port) {
        let details = process_details(pid);
        if !is_jarvis_backend(&details, expected) {
            eprintln!(
                "[backend] port {} is owned by pid {} ({}) - leaving it alone",
                port,
                pid,
                details.trim()
            );
            continue;
        }

        eprintln!("[backend] stopping stale jarvis backend (pid {})", pid);
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

/// 32 random bytes from the OS CSPRNG, hex encoded (64 chars).
///
/// The token is the only thing standing between the local API - which can run
/// shell commands - and every other process on the machine, so it must not be
/// guessable and must not be derived from time or a PID.
#[cfg(windows)]
fn generate_api_token() -> String {
    use windows_sys::Win32::Security::Cryptography::{
        BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
    };

    let mut bytes = [0u8; 32];
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status != 0 {
        // A failure here means no OS entropy: better not to start than to fall
        // back to something predictable and quietly drop the protection.
        panic!("BCryptGenRandom failed with NTSTATUS {status:#x}");
    }
    hex(&bytes)
}

#[cfg(not(windows))]
fn generate_api_token() -> String {
    // Not a supported release target, but the token must never be guessable:
    // time-derived bytes would defeat the whole local API protection. Use the
    // OS CSPRNG and fail loudly if it is unavailable, like the Windows branch.
    use std::fs::File;
    let mut bytes = [0u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .expect("/dev/urandom unavailable; refusing to create a guessable API token");
    hex(&bytes)
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

fn start_python_backend(
    commands: &[BackendCommand],
    data_dir: &Path,
    workdir: Option<&Path>,
    api_token: &str,
) -> (Option<Child>, BackendStatus) {
    let mode = commands
        .first()
        .map(|c| c.mode)
        .unwrap_or("none")
        .to_string();

    if port_is_open(BACKEND_PORT) {
        // A previous run that was killed with Ctrl+C (or crashed) leaves its
        // python child alive on the port. We take the port back, but only from
        // a process we can identify as *our* backend: stop_stale_backend checks
        // the command line before killing anything.
        // Only the exact scripts this build would launch may be terminated.
        let expected: Vec<PathBuf> = commands.iter().map(|c| c.server_py.clone()).collect();
        stop_stale_backend(BACKEND_PORT, &expected);

        if port_is_open(BACKEND_PORT) {
            let message = format!(
                "Port {} is already used by another program, so the local backend cannot start. \
                 Close that program and reopen Jarvis.",
                BACKEND_PORT
            );
            eprintln!("[backend] {}", message);
            return (
                None,
                BackendStatus {
                    ready: false,
                    failed: true,
                    message,
                    mode,
                },
            );
        }
    }

    // Python tracebacks are invisible in a GUI build: keep them in a file the
    // error message can point the user at.
    let log_path = backend_log_path(data_dir);
    if let Ok(mut header) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
    {
        let _ = writeln!(header, "\n--- jarvis backend start ---");
    }
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .ok();

    let mut last_error = String::new();
    for spec in commands {
        let mut cmd = Command::new(&spec.program);
        cmd.arg(&spec.server_py)
            .current_dir(&spec.cwd)
            .env("JARVIS_DATA_DIR", data_dir)
            .env("JARVIS_API_TOKEN", api_token)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::null());

        // The workspace is optional: only pass it when the user picked one.
        if let Some(dir) = workdir {
            cmd.env("JARVIS_WORKDIR", dir);
        }

        if let Some(file) = &log {
            if let Ok(copy) = file.try_clone() {
                cmd.stdout(Stdio::from(copy));
            }
            if let Ok(copy) = file.try_clone() {
                cmd.stderr(Stdio::from(copy));
            }
        }

        match cmd.spawn() {
            Ok(child) => {
                eprintln!(
                    "[backend] started {} {} ({}, log: {})",
                    spec.program.display(),
                    spec.server_py.display(),
                    spec.mode,
                    backend_log_path(data_dir).display()
                );
                return (
                    Some(child),
                    BackendStatus {
                        ready: false,
                        failed: false,
                        message: String::new(),
                        mode,
                    },
                );
            }
            Err(err) => {
                eprintln!(
                    "[backend] failed to start {}: {}",
                    spec.program.display(),
                    err
                );
                last_error = err.to_string();
            }
        }
    }

    let message = format!(
        "The local backend could not be started ({}). Reinstall Jarvis; if antivirus \
         quarantined backend\\python\\python.exe, restore it. Details: {}",
        last_error,
        backend_log_path(data_dir).display()
    );
    eprintln!("[backend] {}", message);
    (
        None,
        BackendStatus {
            ready: false,
            failed: true,
            message,
            mode,
        },
    )
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
    *lock(&state.assistant_visible) = true;

    // Console mode: the assistant is the whole picture now, so the small
    // windows step aside instead of fighting it for screen space.
    let settings_was_open = *lock(&state.settings_visible);
    *lock(&state.settings_before_console) = settings_was_open;
    *lock(&state.settings_visible) = false;

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
    *lock(&state.assistant_visible) = false;

    if let Some(assistant) = app.get_webview_window("assistant") {
        let _ = assistant.hide();
    }

    let settings_back = *lock(&state.settings_before_console);
    *lock(&state.settings_visible) = settings_back;

    if let Some(main) = app.get_webview_window("main") {
        let _ = main.show();
        let _ = main.set_focus();
    }

    if settings_back {
        if let (Some(main), Some(settings)) = (
            app.get_webview_window("main"),
            app.get_webview_window("settings"),
        ) {
            let gap = *lock(&state.gap);
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

/// Quit the whole application. The main window's close button calls this so the
/// hidden assistant/settings windows cannot keep the process alive.
#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    app.exit(0);
}

#[tauri::command]
fn show_settings(app: tauri::AppHandle, state: State<'_, AppState>) {
    *lock(&state.settings_visible) = true;
    if let (Some(main), Some(settings)) = (
        app.get_webview_window("main"),
        app.get_webview_window("settings"),
    ) {
        let gap = *lock(&state.gap);
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
            .map(|s| *lock(&s.assistant_visible))
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
    *lock(&state.settings_visible) = false;
    if let Some(settings) = app.get_webview_window("settings") {
        let _ = settings.hide();
    }
}

#[tauri::command]
fn get_gap(state: State<'_, AppState>) -> f64 {
    *lock(&state.gap)
}

#[tauri::command]
fn set_gap(app: tauri::AppHandle, state: State<'_, AppState>, gap: f64) {
    *lock(&state.gap) = gap;
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
    *lock(&state.opacity)
}

#[tauri::command]
fn set_opacity(app: tauri::AppHandle, state: State<'_, AppState>, opacity: f64) {
    let opacity = opacity.clamp(0.1, 1.0);
    *lock(&state.opacity) = opacity;
    save_opacity(&app, opacity);
    let _ = app.emit("opacity-changed", opacity);
}

#[tauri::command]
fn get_theme(state: State<'_, AppState>) -> String {
    lock(&state.theme).clone()
}

#[tauri::command]
fn set_theme(app: tauri::AppHandle, state: State<'_, AppState>, theme: String) {
    let theme = normalize_theme(&theme);
    *lock(&state.theme) = theme.clone();
    save_theme(&app, &theme);
    let _ = app.emit("theme-changed", theme);
}

#[tauri::command]
fn get_config(state: State<'_, AppState>) -> AppConfig {
    lock(&state.config).clone()
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

    // STT/TTS settings may be written by the Python API rather than the UI.
    // An AppConfig payload that omits them must not delete them on disk.
    if merged.stt.is_none() {
        merged.stt = on_disk.stt.clone();
    }
    if merged.tts.is_none() {
        merged.tts = on_disk.tts.clone();
    }

    let mut config = lock(&state.config);
    *config = merged.clone();
    save_config(&app, &merged);
}

#[tauri::command]
fn get_workdir(app: tauri::AppHandle) -> String {
    // Empty string means the user has not chosen a workspace. There is no
    // default: the UI shows a "workspace" placeholder and the agent simply
    // works with absolute paths.
    load_config(&app).workdir.unwrap_or_default()
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

#[tauri::command]
fn backend_status(state: State<AppState>) -> BackendStatus {
    state
        .backend
        .lock()
        .map(|s| s.clone())
        .unwrap_or_else(|_| BackendStatus {
            ready: false,
            failed: true,
            message: "Backend state is unavailable.".to_string(),
            mode: String::new(),
        })
}

/// The per-launch token the UI must send as `X-Jarvis-Token`.
///
/// Calls come only from the app's own WebView; the command is not exposed to
/// the network. It stays in memory for the lifetime of the process.
#[tauri::command]
fn api_token(state: State<'_, AppState>) -> String {
    state.api_token.clone()
}

fn main() {
    tauri::Builder::default()
        // Must be registered before other plugins: the second instance should
        // do nothing but hand focus to the first one - no second backend, no
        // second token fighting over port 8765.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let data = data_dir();
            let _ = std::fs::create_dir_all(&data);
            let workdir = load_config_at(&data).workdir.map(PathBuf::from);

            // Token rotation: check if stored token is expired, rotate if needed
            let api_token = if is_token_expired() {
                eprintln!("[security] Stored token expired, generating new token");
                let (token, expiry) = generate_and_save_token();
                eprintln!("[security] New token generated, expires at {}", expiry);
                token
            } else if let Some((_, expiry)) = load_token_info() {
                // Token exists and not expired, but we need to generate a new one for this session
                // since the Python backend expects a fresh token per launch
                // Actually, we should use the stored token to maintain continuity
                // But the token is per-launch... let's generate new but save expiry
                eprintln!("[security] Generating new session token (previous expiry: {})", expiry);
                let (token, new_expiry) = generate_and_save_token();
                eprintln!("[security] New token generated, expires at {}", new_expiry);
                token
            } else {
                // No stored token, generate new
                eprintln!("[security] No stored token, generating new token");
                let (token, expiry) = generate_and_save_token();
                eprintln!("[security] New token generated, expires at {}", expiry);
                token
            };

            let (child, status) = match resolve_backend(app.handle()) {
                Ok(commands) => {
                    start_python_backend(&commands, &data, workdir.as_deref(), &api_token)
                }
                Err(message) => {
                    eprintln!("[backend] {}", message);
                    (
                        None,
                        BackendStatus {
                            ready: false,
                            failed: true,
                            message,
                            mode: "none".to_string(),
                        },
                    )
                }
            };
            app.manage(PythonServer(Mutex::new(child)));

            let config = load_config(app.handle());
            let gap = load_gap(app.handle());
            let opacity = load_opacity(app.handle());
            let theme = load_theme(app.handle());

            app.manage(AppState {
                gap: Mutex::new(gap),
                opacity: Mutex::new(opacity),
                theme: Mutex::new(theme.clone()),
                assistant_visible: Mutex::new(false),
                settings_visible: Mutex::new(false),
                settings_before_console: Mutex::new(false),
                config: Mutex::new(config),
                backend: Mutex::new(status.clone()),
                api_token,
            });

            // Started after AppState exists so the first status update lands.
            spawn_backend_watchdog(app.handle().clone(), status);

            // Apply both settings before React paints, so every window starts
            // in the saved palette instead of flashing the default one.
            let _js = format!(
                "document.documentElement.dataset.theme='{}';document.documentElement.style.setProperty('--jarvis-opacity','{}');document.querySelectorAll('[data-panel-bg]').forEach(function(e){{e.style.background='rgba(var(--jarvis-panel-rgb), {})'}});",
                theme, opacity, opacity
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
        // WebView2 has no visible permission bubble in a borderless Tauri
        // window. Without an explicit handler, `getUserMedia` for the
        // microphone is denied (or silently defaulted), so no audio was ever
        // captured and no tray mic indicator appeared. The webview only ever
        // loads our own app origin, so approving microphone here is safe.
        .on_permission_request(|_webview, kind| match kind {
            PermissionKind::Microphone => PermissionResponse::Allow,
            _ => PermissionResponse::Default,
        })
        .invoke_handler(tauri::generate_handler![
            move_group,
            show_assistant,
            hide_assistant,
            restore_desktop,
            quit_app,
            show_settings,
            hide_settings,
            get_gap,
            set_gap,
            get_opacity,
            set_opacity,
            get_theme,
            set_theme,
            get_config,
            set_config,
            get_workdir,
            set_workdir,
            backend_status,
            api_token,
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

    #[test]
    fn api_tokens_are_64_hex_chars_and_unique() {
        let a = generate_api_token();
        let b = generate_api_token();
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn only_a_jarvis_server_main_py_counts_as_a_stale_backend() {
        let bundled = "python.exe\n\"c:\\users\\me\\appdata\\local\\jarvis\\backend\\python\\python.exe\" \
                       \"c:\\users\\me\\appdata\\local\\jarvis\\backend\\app\\server\\main.py\"";
        let bundled_path = PathBuf::from(r"c:\users\me\appdata\local\jarvis\backend\app\server\main.py");
        let dev_path = PathBuf::from(r"C:\repo\jarvis\server\main.py");
        let expected = [bundled_path, dev_path.clone()];

        assert!(is_jarvis_backend(bundled, &expected));
        assert!(is_jarvis_backend("python\nc:/repo/jarvis/server/main.py", &expected));

        // A python that is not our backend, or our path in something else.
        assert!(!is_jarvis_backend("python.exe\nc:\\tools\\http_server.py", &expected));
        assert!(!is_jarvis_backend(
            "node.exe\n\"c:\\repo\\jarvis\\server\\main.py\"",
            &expected,
        ));
        assert!(!is_jarvis_backend("", &expected));
    }
}
