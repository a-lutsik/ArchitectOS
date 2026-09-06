#!/usr/bin/env bash
# Install ArchitectOS HTTP server and register login/boot autostart (macOS / Linux).
# Usage (from an unpacked share package or repo):
#   ./scripts/install_autostart.sh
#   ./install.sh          # when run from dist/share stage
# Env:
#   ARCHITECTOS_ROOT   — data root (default: ~/ArchitectOS)
#   ARCHITECTOS_PORT   — preferred port (default: 8766)
#   ARCHITECTOS_SERVER_BIN — path to architectos-server binary (optional)
#   ARCHITECTOS_SKIP_AUTOSTART=1 — copy files only (no LaunchAgent/systemd)
#   ARCHITECTOS_PROBE_TIMEOUT — seconds for sqlite-vec probe (default 45)
#   ARCHITECTOS_SKIP_PAUSE=1 — do not wait for Enter (CI / tests)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Share package layout: install.sh next to architectos-server
# Repo layout: scripts/install_autostart.sh
PACKAGE_DIR="$SCRIPT_DIR"
if [[ "$(basename "$SCRIPT_DIR")" == "scripts" ]]; then
  PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

wait_if_interactive() {
  if [[ "${ARCHITECTOS_SKIP_PAUSE:-}" == "1" ]]; then
    return 0
  fi
  if [[ -t 0 && -t 1 ]]; then
    echo
    read -r -p "Press Enter to close... / Нажмите Enter, чтобы закрыть..." || true
  fi
}

die() {
  echo "error: $*" >&2
  echo
  echo "========================================"
  echo "  ERROR / ОШИБКА"
  echo "========================================"
  echo "  $*" >&2
  wait_if_interactive
  exit 1
}

uname_s="$(uname -s)"
case "$uname_s" in
  Darwin|Linux) ;;
  *) die "This script supports macOS and Linux. On Windows use install_autostart.ps1 / install.bat." ;;
esac

ROOT="${ARCHITECTOS_ROOT:-$HOME/ArchitectOS}"
PORT="${ARCHITECTOS_PORT:-8766}"
BIN_DIR="$ROOT/bin"
DATA_DIR="$ROOT/data"
LOG_DIR="$ROOT/logs"
LABEL="com.architectos.server"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SYSTEMD_UNIT="$HOME/.config/systemd/user/architectos-server.service"

resolve_server_bin() {
  if [[ -n "${ARCHITECTOS_SERVER_BIN:-}" && -f "$ARCHITECTOS_SERVER_BIN" ]]; then
    echo "$ARCHITECTOS_SERVER_BIN"
    return
  fi
  local candidates=(
    "$PACKAGE_DIR/architectos-server"
    "$PACKAGE_DIR/bin/architectos-server"
    "$BIN_DIR/architectos-server"
  )
  for c in "${candidates[@]}"; do
    if [[ -f "$c" ]]; then
      echo "$c"
      return
    fi
  done
  die "architectos-server not found next to this script. Build with scripts/build_share_package.py or set ARCHITECTOS_SERVER_BIN."
}

SRC_BIN="$(resolve_server_bin)"
mkdir -p "$BIN_DIR" "$DATA_DIR" "$LOG_DIR"
DEST_BIN="$BIN_DIR/architectos-server"
cp -f "$SRC_BIN" "$DEST_BIN"
chmod +x "$DEST_BIN"
if [[ "$uname_s" == "Darwin" ]]; then
  xattr -dr com.apple.quarantine "$DEST_BIN" 2>/dev/null || true
fi
if [[ ! -x "$DEST_BIN" ]]; then
  die "copied binary is not executable: $DEST_BIN"
fi
echo "Installed server → $DEST_BIN"
echo "Data root        → $ROOT"

install_companions() {
  if [[ -f "$PACKAGE_DIR/uninstall.sh" ]]; then
    cp -f "$PACKAGE_DIR/uninstall.sh" "$ROOT/uninstall.sh"
    chmod +x "$ROOT/uninstall.sh"
  elif [[ -f "$SCRIPT_DIR/uninstall_autostart.sh" ]]; then
    cp -f "$SCRIPT_DIR/uninstall_autostart.sh" "$ROOT/uninstall.sh"
    chmod +x "$ROOT/uninstall.sh"
  fi
  if [[ -d "$PACKAGE_DIR/docs" ]]; then
    mkdir -p "$ROOT/docs"
    for f in "$PACKAGE_DIR/docs/"*; do
      [[ -f "$f" ]] || continue
      cp -f "$f" "$ROOT/docs/"
    done
  fi
  local name mcp
  for name in README-SHARE.txt ЧИТАЙМЕНЯ.txt README-mcp.txt; do
    if [[ -f "$PACKAGE_DIR/$name" ]]; then
      cp -f "$PACKAGE_DIR/$name" "$ROOT/$name"
    fi
  done
  for mcp in architectos-mcp architectos-mcp.exe; do
    if [[ -f "$PACKAGE_DIR/$mcp" ]]; then
      cp -f "$PACKAGE_DIR/$mcp" "$BIN_DIR/$mcp"
      chmod +x "$BIN_DIR/$mcp" 2>/dev/null || true
      if [[ "$uname_s" == "Darwin" ]]; then
        xattr -dr com.apple.quarantine "$BIN_DIR/$mcp" 2>/dev/null || true
      fi
      echo "Installed MCP    → $BIN_DIR/$mcp"
    fi
  done
  if [[ -d "$PACKAGE_DIR/ide" ]]; then
    mkdir -p "$ROOT/ide"
    cp -R "$PACKAGE_DIR/ide/." "$ROOT/ide/"
    echo "Installed IDE zip → $ROOT/ide"
  fi
}
install_companions

PROBE_TIMEOUT="${ARCHITECTOS_PROBE_TIMEOUT:-45}"
probe_vector_runtime() {
  local bin="$1"
  local timeout_s="${PROBE_TIMEOUT}"
  local out="$LOG_DIR/vector-runtime.probe.log"
  local pid elapsed=0 rc=0
  : >"$out"
  "$bin" vector-runtime >"$out" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$elapsed" -ge "$timeout_s" ]]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.2
      kill -9 "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      echo "sqlite-vec probe timed out; continuing install." >&2
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  wait "$pid" || rc=$?
  cat "$out" 2>/dev/null || true
  return "$rc"
}

echo
echo "==> Memory search (sqlite-vec)"
if probe_vector_runtime "$DEST_BIN"; then
  :
else
  echo "sqlite-vec probe unavailable in this binary; Ask/Search still work (Python fallback)."
fi

# Interactive prompts (TTY only). Env overrides:
#   ARCHITECTOS_SKIP_AUTOSTART=1 — never register login autostart
#   ARCHITECTOS_NO_START_NOW=1   — install files but do not start server now
ASK_AUTOSTART=1
ASK_START_NOW=1
if [[ "${ARCHITECTOS_SKIP_AUTOSTART:-}" == "1" ]]; then
  ASK_AUTOSTART=0
fi
if [[ "${ARCHITECTOS_NO_START_NOW:-}" == "1" ]]; then
  ASK_START_NOW=0
fi
if [[ -t 0 && -t 1 && "${ARCHITECTOS_SKIP_PAUSE:-}" != "1" ]]; then
  echo
  echo "Setup choices / Параметры установки"
  read -r -p "Start ArchitectOS server now? [Y/n] / Запустить сервер сейчас? [Y/n] " ans_now || true
  case "${ans_now:-Y}" in
    n|N|no|No|NO) ASK_START_NOW=0 ;;
    *) ASK_START_NOW=1 ;;
  esac
  read -r -p "Start server automatically at login? [Y/n] / Автозапуск при входе в систему? [Y/n] " ans_auto || true
  case "${ans_auto:-Y}" in
    n|N|no|No|NO) ASK_AUTOSTART=0 ;;
    *) ASK_AUTOSTART=1 ;;
  esac
fi

if [[ "$ASK_AUTOSTART" != "1" ]]; then
  echo
  echo "Skipped OS autostart."
  echo "Start later: ARCHITECTOS_ROOT=\"${ROOT}\" \"${DEST_BIN}\" --no-browser --host 127.0.0.1 --port ${PORT}"
  if [[ "$ASK_START_NOW" == "1" ]]; then
    ARCHITECTOS_ROOT="$ROOT" nohup "$DEST_BIN" --no-browser --host 127.0.0.1 --port "$PORT" \
      >>"$LOG_DIR/server.stdout.log" 2>>"$LOG_DIR/server.stderr.log" &
    echo "Started server now (pid $!)."
  fi
else
# Stop previous instance if any
if [[ "$uname_s" == "Darwin" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl unload "$PLIST" 2>/dev/null || true
elif command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop architectos-server.service 2>/dev/null || true
fi

if [[ "$uname_s" == "Darwin" ]]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${DEST_BIN}</string>
    <string>--no-browser</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ARCHITECTOS_ROOT</key>
    <string>${ROOT}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/server.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/server.stderr.log</string>
</dict>
</plist>
EOF
  echo "Autostart registered: LaunchAgent ${LABEL}"
  echo "  plist → $PLIST"
  if [[ "$ASK_START_NOW" == "1" ]]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
      || launchctl load -w "$PLIST"
    echo "Started server now via LaunchAgent."
  else
    echo "Server will start at the next login (plist written, not loaded now)."
  fi
elif command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$(dirname "$SYSTEMD_UNIT")"
  cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=ArchitectOS local HTTP server
After=default.target

[Service]
Type=simple
Environment=ARCHITECTOS_ROOT=${ROOT}
WorkingDirectory=${ROOT}
ExecStart=${DEST_BIN} --no-browser --host 127.0.0.1 --port ${PORT}
Restart=on-failure
RestartSec=5
StandardOutput=append:${LOG_DIR}/server.stdout.log
StandardError=append:${LOG_DIR}/server.stderr.log

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable architectos-server.service
  if [[ "$ASK_START_NOW" == "1" ]]; then
    systemctl --user start architectos-server.service
    echo "Autostart registered + started: systemd --user architectos-server.service"
  else
    echo "Autostart registered (starts at next login): systemd --user architectos-server.service"
  fi
else
  # Generic login-item fallback via shell profile snippet
  MARKER="# ArchitectOS autostart"
  PROFILE="$HOME/.zprofile"
  [[ -f "$HOME/.bash_profile" ]] && PROFILE="$HOME/.bash_profile"
  if ! grep -qF "$MARKER" "$PROFILE" 2>/dev/null; then
    cat >> "$PROFILE" <<EOF

${MARKER}
if [ -x "${DEST_BIN}" ]; then
  ARCHITECTOS_ROOT="${ROOT}" nohup "${DEST_BIN}" --no-browser --host 127.0.0.1 --port ${PORT} \\
    >>"${LOG_DIR}/server.stdout.log" 2>>"${LOG_DIR}/server.stderr.log" &
fi
EOF
  fi
  if [[ "$ASK_START_NOW" == "1" ]]; then
    ARCHITECTOS_ROOT="$ROOT" nohup "$DEST_BIN" --no-browser --host 127.0.0.1 --port "$PORT" \
      >>"$LOG_DIR/server.stdout.log" 2>>"$LOG_DIR/server.stderr.log" &
    echo "Autostart registered via $PROFILE; started now (pid $!)"
  else
    echo "Autostart registered via $PROFILE (starts at next shell login)"
  fi
fi
fi

UNINSTALL_HINT="$ROOT/uninstall.sh"
if [[ ! -f "$UNINSTALL_HINT" ]]; then
  UNINSTALL_HINT="$SCRIPT_DIR/uninstall_autostart.sh"
  if [[ -f "$PACKAGE_DIR/uninstall.sh" ]]; then
    UNINSTALL_HINT="$PACKAGE_DIR/uninstall.sh"
  fi
fi

echo
echo "========================================"
echo "  SUCCESS / УСПЕХ"
echo "========================================"
if [[ "$ASK_AUTOSTART" == "1" ]]; then
  echo "Server will start at login/boot."
else
  echo "Files installed under ${ROOT} (no login autostart)."
fi
echo "UI: http://127.0.0.1:${PORT}/"
echo "Runtime state: ${DATA_DIR}/architectos.runtime.json"
echo "Uninstall: ${UNINSTALL_HINT}"
if [[ -f "$ROOT/docs/GUIDE_RU.md" ]]; then
  echo "Guide (RU): ${ROOT}/docs/GUIDE_RU.md"
else
  echo "Guide (RU): docs/GUIDE_RU.md"
fi
wait_if_interactive
