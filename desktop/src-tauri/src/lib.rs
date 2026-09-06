//! ArchitectOS desktop shell: spawn the Python server sidecar, load its UI in a webview, kill on quit.

use std::fs;
use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};

const DEFAULT_PORT: u16 = 8766;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(90);
const POLL_INTERVAL: Duration = Duration::from_millis(200);
const AUTOSTART_LABEL: &str = "com.architectos.server";
#[cfg(windows)]
const WIN_TASK_NAME: &str = "ArchitectOS Server";

struct ServerState {
    child: Mutex<Option<Child>>,
    /// When false, Exit must not kill the process (reused LaunchAgent / left for autostart).
    owns_child: Mutex<bool>,
    data_root: Mutex<Option<PathBuf>>,
    ui_url: Mutex<Option<String>>,
}

#[derive(Debug, Deserialize)]
struct RuntimeState {
    url: Option<String>,
    port: Option<u16>,
    host: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DesktopPrefs {
    first_run_complete: bool,
    login_autostart: bool,
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

fn prefs_path(data_root: &Path) -> PathBuf {
    data_root.join("data").join("desktop-prefs.json")
}

fn load_prefs(data_root: &Path) -> DesktopPrefs {
    let path = prefs_path(data_root);
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn save_prefs(data_root: &Path, prefs: &DesktopPrefs) -> Result<(), String> {
    let path = prefs_path(data_root);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| format!("Cannot create {}: {err}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(prefs).map_err(|err| err.to_string())?;
    fs::write(&path, text).map_err(|err| format!("Cannot write {}: {err}", path.display()))
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
            if let Some(contents) = dir.parent() {
                candidates.push(contents.join("Resources").join(bin_name));
                candidates.push(contents.join("Resources").join("binaries").join(bin_name));
            }
        }
    }

    candidates.push(resolve_architectos_root().join("bin").join(bin_name));

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

fn install_server_bin_copy(app: &AppHandle, data_root: &Path) -> Result<PathBuf, String> {
    let src = find_sidecar(app).ok_or_else(|| {
        "architectos-server sidecar not found. Rebuild with scripts/build_desktop.sh.".to_string()
    })?;
    let bin_dir = data_root.join("bin");
    fs::create_dir_all(&bin_dir).map_err(|err| format!("Cannot create {}: {err}", bin_dir.display()))?;
    let dest = bin_dir.join(sidecar_bin_name());
    if src != dest {
        fs::copy(&src, &dest).map_err(|err| {
            format!(
                "Cannot copy sidecar {} → {}: {err}",
                src.display(),
                dest.display()
            )
        })?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = fs::metadata(&dest)
            .map_err(|err| format!("Cannot stat {}: {err}", dest.display()))?
            .permissions();
        perms.set_mode(0o755);
        let _ = fs::set_permissions(&dest, perms);
        let _ = Command::new("xattr")
            .args(["-dr", "com.apple.quarantine"])
            .arg(&dest)
            .status();
    }
    Ok(dest)
}

fn enable_login_autostart(app: &AppHandle, data_root: &Path, port: u16) -> Result<(), String> {
    let dest_bin = install_server_bin_copy(app, data_root)?;
    let log_dir = data_root.join("logs");
    fs::create_dir_all(&log_dir).map_err(|err| format!("Cannot create {}: {err}", log_dir.display()))?;

    #[cfg(target_os = "macos")]
    {
        let home = dirs_fallback_home();
        let plist_dir = home.join("Library/LaunchAgents");
        fs::create_dir_all(&plist_dir)
            .map_err(|err| format!("Cannot create {}: {err}", plist_dir.display()))?;
        let plist = plist_dir.join(format!("{AUTOSTART_LABEL}.plist"));
        let root = data_root.display().to_string();
        let bin = dest_bin.display().to_string();
        let stdout = log_dir.join("server.stdout.log").display().to_string();
        let stderr = log_dir.join("server.stderr.log").display().to_string();
        let body = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{AUTOSTART_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{bin}</string>
    <string>--no-browser</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>{port}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ARCHITECTOS_ROOT</key>
    <string>{root}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>{root}</string>
  <key>StandardOutPath</key>
  <string>{stdout}</string>
  <key>StandardErrorPath</key>
  <string>{stderr}</string>
</dict>
</plist>
"#
        );
        fs::write(&plist, body).map_err(|err| format!("Cannot write {}: {err}", plist.display()))?;
        let uid = Command::new("id")
            .arg("-u")
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "501".into());
        let domain = format!("gui/{uid}");
        let _ = Command::new("launchctl")
            .args(["bootout", &format!("{domain}/{AUTOSTART_LABEL}")])
            .status();
        let _ = Command::new("launchctl").args(["unload"]).arg(&plist).status();
        // Only load now if nothing is listening — otherwise register for next login.
        if !tcp_ready(port) {
            let loaded = Command::new("launchctl")
                .args(["bootstrap", &domain])
                .arg(&plist)
                .status()
                .map(|s| s.success())
                .unwrap_or(false);
            if !loaded {
                let _ = Command::new("launchctl").args(["load", "-w"]).arg(&plist).status();
            }
        } else {
            // Ensure it is registered for future logins without starting a second instance.
            let _ = Command::new("launchctl").args(["load", "-w"]).arg(&plist).status();
        }
        eprintln!("Login autostart enabled: {}", plist.display());
        return Ok(());
    }

    #[cfg(windows)]
    {
        let wrapper = data_root.join("bin").join("start-architectos-server.cmd");
        let stdout = log_dir.join("server.stdout.log");
        let stderr = log_dir.join("server.stderr.log");
        let script = format!(
            "@echo off\r\nset ARCHITECTOS_ROOT={root}\r\ncd /d \"{root}\"\r\n\"{bin}\" --no-browser --host 127.0.0.1 --port {port} >> \"{stdout}\" 2>> \"{stderr}\"\r\n",
            root = data_root.display(),
            bin = dest_bin.display(),
            port = port,
            stdout = stdout.display(),
            stderr = stderr.display(),
        );
        fs::write(&wrapper, script)
            .map_err(|err| format!("Cannot write {}: {err}", wrapper.display()))?;

        let wrap_escaped = wrapper.display().to_string().replace('\'', "''");
        let ps = format!(
            "$ErrorActionPreference='Stop'; \
             $existing = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; \
             if ($existing) {{ Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false }}; \
             $action = New-ScheduledTaskAction -Execute '{wrap}'; \
             $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; \
             $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable; \
             $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited; \
             Register-ScheduledTask -TaskName '{task}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null; \
             New-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name 'ArchitectOSServer' -Value '\"{wrap}\"' -PropertyType String -Force | Out-Null;",
            task = WIN_TASK_NAME,
            wrap = wrap_escaped,
        );
        let status = Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &ps])
            .status()
            .map_err(|err| format!("Failed to register Windows autostart: {err}"))?;
        if !status.success() {
            return Err("Windows Scheduled Task registration failed.".into());
        }
        eprintln!("Login autostart enabled: Scheduled Task '{WIN_TASK_NAME}'");
        return Ok(());
    }

    #[cfg(not(any(target_os = "macos", windows)))]
    {
        let _ = (app, dest_bin, port);
        Err("Login autostart is only implemented for macOS and Windows.".into())
    }
}

fn disable_login_autostart() {
    #[cfg(target_os = "macos")]
    {
        let plist = dirs_fallback_home()
            .join("Library/LaunchAgents")
            .join(format!("{AUTOSTART_LABEL}.plist"));
        let uid = Command::new("id")
            .arg("-u")
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "501".into());
        let _ = Command::new("launchctl")
            .args(["bootout", &format!("gui/{uid}/{AUTOSTART_LABEL}")])
            .status();
        let _ = Command::new("launchctl").args(["unload"]).arg(&plist).status();
        let _ = fs::remove_file(&plist);
    }
    #[cfg(windows)]
    {
        let ps = format!(
            "Unregister-ScheduledTask -TaskName '{WIN_TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue; \
             Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name 'ArchitectOSServer' -ErrorAction SilentlyContinue"
        );
        let _ = Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &ps])
            .status();
    }
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

fn preferred_port() -> u16 {
    std::env::var("ARCHITECTOS_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

fn try_reuse_running_server(data_root: &Path, port: u16) -> Option<String> {
    if !tcp_ready(port) {
        return None;
    }
    if let Some(state) = read_runtime_state(data_root) {
        if let Some(url) = state.url.filter(|u| !u.is_empty()) {
            return Some(url);
        }
        if let Some(p) = state.port {
            let host = state.host.unwrap_or_else(|| "127.0.0.1".into());
            return Some(format!("http://{host}:{p}"));
        }
    }
    Some(format!("http://127.0.0.1:{port}"))
}

/// Attach to an already-running server (login autostart) or spawn a new sidecar.
fn start_backend(app: &AppHandle) -> Result<(Option<Child>, String, PathBuf, bool), String> {
    let packaged_root = resolve_architectos_root();
    let port = preferred_port();
    fs::create_dir_all(packaged_root.join("data")).map_err(|err| {
        format!(
            "Cannot create data directory {}: {err}",
            packaged_root.join("data").display()
        )
    })?;

    if let Some(url) = try_reuse_running_server(&packaged_root, port) {
        eprintln!("Reusing running ArchitectOS server at {url}");
        return Ok((None, url, packaged_root, false));
    }

    let (mut child, effective_root) = if let Some(bin) = find_sidecar(app) {
        eprintln!("Starting ArchitectOS sidecar: {}", bin.display());
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
        Ok(url) => Ok((Some(child), url, effective_root, true)),
        Err(err) => {
            stop_child(&mut Some(child));
            Err(err)
        }
    }
}

fn navigate_main(app: &AppHandle, url: &str) -> Result<(), String> {
    let parsed = url
        .parse::<url::Url>()
        .map_err(|e| format!("invalid server url {url}: {e}"))?;
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.navigate(parsed);
    } else {
        WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
            .title("ArchitectOS")
            .inner_size(1280.0, 840.0)
            .build()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn show_first_run(app: &AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.close();
    }
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("first-run.html".into()))
        .title("ArchitectOS setup")
        .inner_size(720.0, 640.0)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(())
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

fn apply_backend(
    app: &AppHandle,
    state: &ServerState,
    child: Option<Child>,
    url: String,
    data_root: PathBuf,
    owns: bool,
) -> Result<(), String> {
    if let Ok(mut guard) = state.child.lock() {
        *guard = child;
    }
    if let Ok(mut guard) = state.owns_child.lock() {
        *guard = owns;
    }
    if let Ok(mut guard) = state.data_root.lock() {
        *guard = Some(data_root.clone());
    }
    if let Ok(mut guard) = state.ui_url.lock() {
        *guard = Some(url.clone());
    }
    eprintln!("ArchitectOS UI → {url} (data: {})", data_root.display());
    navigate_main(app, &url)
}

#[tauri::command]
fn complete_first_run(
    app: AppHandle,
    state: State<'_, ServerState>,
    start_now: bool,
    login_autostart: bool,
) -> Result<(), String> {
    let data_root = resolve_architectos_root();
    fs::create_dir_all(data_root.join("data"))
        .map_err(|err| format!("Cannot create data dir: {err}"))?;

    let prefs = DesktopPrefs {
        first_run_complete: true,
        login_autostart,
    };
    save_prefs(&data_root, &prefs)?;

    if login_autostart {
        enable_login_autostart(&app, &data_root, preferred_port())?;
    } else {
        disable_login_autostart();
    }

    if !start_now {
        if let Some(window) = app.get_webview_window("main") {
            let msg = if login_autostart {
                "Login autostart is configured. The server will start at the next login.<br/><br/>Open <b>ArchitectOS</b> from Applications / Start Menu when you want the UI."
            } else {
                "Setup saved. Open ArchitectOS again and choose “Start the local server now” to use the app."
            };
            let html = format!(
                "data:text/html,<!doctype html><html><body style='font-family:system-ui;padding:2rem;background:#111;color:#eee'>\
                 <h1>Setup complete</h1><p>{msg}</p>\
                 <p style='color:#a1a1aa'>You can close this window.</p></body></html>"
            );
            if let Ok(url) = html.parse() {
                let _ = window.navigate(url);
            }
        }
        return Ok(());
    }

    let (child, url, root, owns) = start_backend(&app)?;
    // If login autostart is on, leave the process running after quit so agents keep working.
    let owns = owns && !login_autostart;
    apply_backend(&app, &state, child, url, root, owns)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let server_state = ServerState {
        child: Mutex::new(None),
        owns_child: Mutex::new(false),
        data_root: Mutex::new(None),
        ui_url: Mutex::new(None),
    };

    tauri::Builder::default()
        .manage(server_state)
        .invoke_handler(tauri::generate_handler![complete_first_run])
        .setup(|app| {
            let data_root = resolve_architectos_root();
            let _ = fs::create_dir_all(data_root.join("data"));
            let prefs = load_prefs(&data_root);

            if !prefs.first_run_complete {
                let force = std::env::var("ARCHITECTOS_FORCE_FIRST_RUN").ok().as_deref() == Some("1");
                let packaged = find_sidecar(app.handle()).is_some() && repo_root_from_env().is_none();
                if force || packaged {
                    if let Err(err) = show_first_run(app.handle()) {
                        eprintln!("first-run UI failed: {err}");
                        show_error_window(app.handle(), &err)?;
                    }
                    return Ok(());
                }
            }

            match start_backend(app.handle()) {
                Ok((child, url, root, owns)) => {
                    let owns = owns && !prefs.login_autostart;
                    if let Err(err) = apply_backend(
                        app.handle(),
                        app.state::<ServerState>().inner(),
                        child,
                        url,
                        root,
                        owns,
                    ) {
                        show_error_window(app.handle(), &err)?;
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
                let owns = app_handle
                    .state::<ServerState>()
                    .owns_child
                    .lock()
                    .map(|g| *g)
                    .unwrap_or(false);
                if owns {
                    if let Ok(mut guard) = app_handle.state::<ServerState>().child.lock() {
                        stop_child(&mut guard);
                    }
                }
            }
        });
}
