//! ArchitectOS desktop shell: spawn the Python server sidecar, load its UI in a webview, kill on quit.

use std::fs;
use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

const DEFAULT_PORT: u16 = 8765;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(90);
const POLL_INTERVAL: Duration = Duration::from_millis(200);

struct ServerState {
    child: Mutex<Option<Child>>,
}

#[derive(Debug, Deserialize)]
struct RuntimeState {
    url: Option<String>,
    port: Option<u16>,
    host: Option<String>,
}

fn default_architectos_root() -> PathBuf {
    if cfg!(windows) {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            return PathBuf::from(local).join("ArchitectOS");
        }
        return dirs_fallback_home()
            .join("AppData")
            .join("Local")
            .join("ArchitectOS");
    }
    dirs_fallback_home().join("ArchitectOS")
}

fn dirs_fallback_home() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn resolve_architectos_root() -> PathBuf {
    if let Ok(override_root) = std::env::var("ARCHITECTOS_ROOT") {
        let trimmed = override_root.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    default_architectos_root()
}

fn repo_root_from_env() -> Option<PathBuf> {
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let desktop = PathBuf::from(manifest);
        if let Some(root) = desktop.parent() {
            if root.join("run_architectos.py").is_file() {
                return Some(root.to_path_buf());
            }
        }
    }
    let exe = std::env::current_exe().ok()?;
    for ancestor in exe.ancestors().take(8) {
        if ancestor.join("run_architectos.py").is_file() {
            return Some(ancestor.to_path_buf());
        }
    }
    None
}

fn sidecar_bin_name() -> &'static str {
    if cfg!(windows) {
        "architectos-server.exe"
    } else {
        "architectos-server"
    }
}

fn find_sidecar(app: &AppHandle) -> Option<PathBuf> {
    let bin_name = sidecar_bin_name();
    let mut candidates = Vec::new();

    if let Ok(override_bin) = std::env::var("ARCHITECTOS_SERVER_BIN") {
        let p = PathBuf::from(override_bin.trim());
        if !p.as_os_str().is_empty() {
            candidates.push(p);
        }
    }

    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join(bin_name));
        candidates.push(resource_dir.join("binaries").join(bin_name));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join(bin_name));
            candidates.push(dir.join("binaries").join(bin_name));
        }
    }

    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let binaries = PathBuf::from(manifest).join("binaries");
        candidates.push(binaries.join(bin_name));
        if let Ok(entries) = fs::read_dir(&binaries) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with("architectos-server") && entry.path().is_file() {
                    candidates.push(entry.path());
                }
            }
        }
    }

    candidates.into_iter().find(|p| p.is_file())
}

fn spawn_dev_server(root: &Path, data_root: &Path, port: u16) -> Result<Child, String> {
    let script = root.join("run_architectos.py");
    if !script.is_file() {
        return Err(format!(
            "Dev fallback needs {} (or set ARCHITECTOS_SERVER_BIN to a built sidecar).",
            script.display()
        ));
    }

    let script_str = script.to_string_lossy().to_string();
    let port_str = port.to_string();
    let args_tail = [
        script_str.as_str(),
        "--no-browser",
        "--host",
        "127.0.0.1",
        "--port",
        port_str.as_str(),
    ];

    let mut attempts: Vec<(&str, Command)> = Vec::new();
    if cfg!(windows) {
        let mut py = Command::new("py");
        py.arg("-3");
        for a in &args_tail {
            py.arg(a);
        }
        attempts.push(("py -3", py));
        let mut python = Command::new("python");
        for a in &args_tail {
            python.arg(a);
        }
        attempts.push(("python", python));
    } else {
        let mut python3 = Command::new("python3");
        for a in &args_tail {
            python3.arg(a);
        }
        attempts.push(("python3", python3));
    }

    let mut last_err = String::from("no Python interpreter found");
    for (label, mut cmd) in attempts {
        cmd.current_dir(root)
            .env("ARCHITECTOS_ROOT", data_root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        match cmd.spawn() {
            Ok(child) => return Ok(child),
            Err(err) => last_err = format!("{label}: {err}"),
        }
    }
    Err(format!(
        "Failed to start ArchitectOS server from source ({last_err}). Install Python 3.12+."
    ))
}

fn spawn_sidecar_bin(bin: &Path, data_root: &Path, port: u16) -> Result<Child, String> {
    Command::new(bin)
        .args([
            "--no-browser",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .env("ARCHITECTOS_ROOT", data_root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Failed to spawn sidecar {}: {err}", bin.display()))
}

fn pipe_child_logs(child: &mut Child) {
    if let Some(stdout) = child.stdout.take() {
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().flatten() {
                eprintln!("[architectos-server] {line}");
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().flatten() {
                eprintln!("[architectos-server] {line}");
            }
        });
    }
}

fn read_runtime_state(data_root: &Path) -> Option<RuntimeState> {
    let path = data_root.join("data").join("architectos.runtime.json");
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn wait_for_server(data_root: &Path, preferred_port: u16) -> Result<String, String> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if let Some(state) = read_runtime_state(data_root) {
            if let Some(url) = state.url.filter(|u| !u.is_empty()) {
                let port = state.port.or_else(|| {
                    url.rsplit_once(':')
                        .and_then(|(_, p)| p.trim_end_matches('/').parse().ok())
                });
                if let Some(port) = port {
                    if tcp_ready(port) {
                        return Ok(url);
                    }
                } else {
                    return Ok(url);
                }
            }
            if let Some(port) = state.port {
                let host = state.host.unwrap_or_else(|| "127.0.0.1".into());
                if tcp_ready(port) {
                    return Ok(format!("http://{host}:{port}"));
                }
            }
        }
        if tcp_ready(preferred_port) {
            return Ok(format!("http://127.0.0.1:{preferred_port}"));
        }
        thread::sleep(POLL_INTERVAL);
    }
    Err(format!(
        "Timed out waiting for ArchitectOS server (checked {}/data/architectos.runtime.json).",
        data_root.display()
    ))
}

fn tcp_ready(port: u16) -> bool {
    let Ok(addr) = format!("127.0.0.1:{port}").parse() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(100)).is_ok()
}

fn stop_child(child: &mut Option<Child>) {
    if let Some(mut process) = child.take() {
        let _ = process.kill();
        let _ = process.wait();
    }
}

fn start_backend(app: &AppHandle) -> Result<(Child, String, PathBuf), String> {
    let packaged_root = resolve_architectos_root();
    let port: u16 = std::env::var("ARCHITECTOS_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_PORT);

    let (mut child, effective_root) = if let Some(bin) = find_sidecar(app) {
        eprintln!("Starting ArchitectOS sidecar: {}", bin.display());
        fs::create_dir_all(packaged_root.join("data")).map_err(|err| {
            format!(
                "Cannot create data directory {}: {err}",
                packaged_root.join("data").display()
            )
        })?;
        (
            spawn_sidecar_bin(&bin, &packaged_root, port)?,
            packaged_root.clone(),
        )
    } else if let Some(repo) = repo_root_from_env() {
        eprintln!(
            "Sidecar binary not found; starting from source: {}",
            repo.display()
        );
        let effective_root = if std::env::var("ARCHITECTOS_ROOT").is_ok() {
            packaged_root.clone()
        } else {
            repo.clone()
        };
        fs::create_dir_all(effective_root.join("data")).map_err(|err| {
            format!(
                "Cannot create data directory {}: {err}",
                effective_root.join("data").display()
            )
        })?;
        (
            spawn_dev_server(&repo, &effective_root, port)?,
            effective_root,
        )
    } else {
        return Err(
            "architectos-server sidecar not found. Build it with scripts/build_desktop.sh \
             (requires PyInstaller + Rust + Tauri CLI), or run `cargo tauri dev` from a full \
             ArchitectOS checkout with Python 3.12+."
                .into(),
        );
    };

    pipe_child_logs(&mut child);
    match wait_for_server(&effective_root, port) {
        Ok(url) => Ok((child, url, effective_root)),
        Err(err) => {
            stop_child(&mut Some(child));
            Err(err)
        }
    }
}

fn show_error_window(app: &AppHandle, message: &str) -> tauri::Result<()> {
    let escaped = message
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;");
    let html = format!(
        "data:text/html,<!doctype html><html><body style='font-family:system-ui;padding:2rem;\
         background:#111;color:#eee'><h1>ArchitectOS failed to start</h1>\
         <pre style='white-space:pre-wrap'>{escaped}</pre>\
         <p>See docs/INSTALLERS.md for prerequisites.</p></body></html>"
    );
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(url) = html.parse() {
            let _ = window.navigate(url);
        }
    } else if let Ok(url) = html.parse::<url::Url>() {
        WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
            .title("ArchitectOS")
            .inner_size(720.0, 480.0)
            .build()?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let server_state = ServerState {
        child: Mutex::new(None),
    };

    tauri::Builder::default()
        .manage(server_state)
        .setup(|app| {
            match start_backend(app.handle()) {
                Ok((child, url, data_root)) => {
                    if let Ok(mut guard) = app.state::<ServerState>().child.lock() {
                        *guard = Some(child);
                    }
                    eprintln!("ArchitectOS UI → {url} (data: {})", data_root.display());
                    let parsed = url
                        .parse::<url::Url>()
                        .map_err(|e| format!("invalid server url {url}: {e}"))?;
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.navigate(parsed);
                    } else {
                        WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
                            .title("ArchitectOS")
                            .inner_size(1280.0, 840.0)
                            .build()?;
                    }
                }
                Err(err) => {
                    eprintln!("ArchitectOS startup failed: {err}");
                    show_error_window(app.handle(), &err)?;
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building ArchitectOS")
        .run(|app_handle, event| {
            if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
                if let Ok(mut guard) = app_handle.state::<ServerState>().child.lock() {
                    stop_child(&mut guard);
                }
            }
        });
}
