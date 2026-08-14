#!/usr/bin/env bash
# Install ArchitectOS HTTP server and register login/boot autostart (macOS / Linux).
# Usage (from an unpacked share package or repo):
#   ./scripts/install_autostart.sh
#   ./install.sh          # when run from dist/share stage
# Env:
#   ARCHITECTOS_ROOT   — data root (default: ~/ArchitectOS)
#   ARCHITECTOS_PORT   — preferred port (default: 8765)
#   ARCHITECTOS_SERVER_BIN — path to architectos-server binary (optional)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Share package layout: install.sh next to architectos-server
# Repo layout: scripts/install_autostart.sh
PACKAGE_DIR="$SCRIPT_DIR"
if [[ "$(basename "$SCRIPT_DIR")" == "scripts" ]]; then
  PACKAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

die() {
  echo "error: $*" >&2
  exit 1
}

uname_s="$(uname -s)"
case "$uname_s" in
  Darwin|Linux) ;;
  *) die "This script supports macOS and Linux. On Windows use install_autostart.ps1 / install.bat." ;;
esac

ROOT="${ARCHITECTOS_ROOT:-$HOME/ArchitectOS}"
PORT="${ARCHITECTOS_PORT:-8765}"
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
echo "Installed server → $DEST_BIN"
echo "Data root        → $ROOT"

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
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    || launchctl load -w "$PLIST"
  echo "Autostart registered: LaunchAgent ${LABEL}"
  echo "  plist → $PLIST"
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
  systemctl --user enable --now architectos-server.service
  echo "Autostart registered: systemd --user architectos-server.service"
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
  ARCHITECTOS_ROOT="$ROOT" nohup "$DEST_BIN" --no-browser --host 127.0.0.1 --port "$PORT" \
    >>"$LOG_DIR/server.stdout.log" 2>>"$LOG_DIR/server.stderr.log" &
  echo "Autostart registered via $PROFILE (no LaunchAgent/systemd available)"
fi

UNINSTALL_HINT="$SCRIPT_DIR/uninstall_autostart.sh"
if [[ -f "$PACKAGE_DIR/uninstall.sh" ]]; then
  UNINSTALL_HINT="$PACKAGE_DIR/uninstall.sh"
fi

echo
echo "Server will start at login/boot. UI: http://127.0.0.1:${PORT}/"
echo "Runtime state: ${DATA_DIR}/architectos.runtime.json"
echo "Uninstall: ${UNINSTALL_HINT}"
echo "Guide (RU): docs/GUIDE_RU.md"
