#!/usr/bin/env bash
# Remove ArchitectOS server login/boot autostart (macOS / Linux).
set -euo pipefail

ROOT="${ARCHITECTOS_ROOT:-$HOME/ArchitectOS}"
LABEL="com.architectos.server"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SYSTEMD_UNIT="$HOME/.config/systemd/user/architectos-server.service"
BIN="$ROOT/bin/architectos-server"

uname_s="$(uname -s)"

if [[ "$uname_s" == "Darwin" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed LaunchAgent ${LABEL}"
elif command -v systemctl >/dev/null 2>&1 && [[ -f "$SYSTEMD_UNIT" ]]; then
  systemctl --user disable --now architectos-server.service 2>/dev/null || true
  rm -f "$SYSTEMD_UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removed systemd user unit architectos-server.service"
fi

# Stop a manually started process if pid file / runtime json points at it
RUNTIME="$ROOT/data/architectos.runtime.json"
if [[ -f "$RUNTIME" ]]; then
  pid="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('pid') or '')" "$RUNTIME" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "Stopped server pid $pid"
  fi
fi

# Optional: keep data, remove only the binary copy
if [[ "${ARCHITECTOS_REMOVE_BIN:-}" == "1" && -f "$BIN" ]]; then
  rm -f "$BIN"
  echo "Removed $BIN"
fi

# Strip shell-profile fallback marker block (best-effort)
for profile in "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.profile"; do
  if [[ -f "$profile" ]] && grep -qF "# ArchitectOS autostart" "$profile"; then
    # macOS/BSD sed needs backup arg; use temp rewrite for portability
    awk '
      /# ArchitectOS autostart/ {skip=1; next}
      skip && /^[[:space:]]*$/ {skip=0; next}
      skip && /^if / {next}
      skip && /nohup/ {next}
      skip && /^fi$/ {skip=0; next}
      skip {next}
      {print}
    ' "$profile" > "${profile}.aos.tmp" && mv "${profile}.aos.tmp" "$profile"
    echo "Cleaned autostart snippet from $profile"
  fi
done

echo "Autostart disabled. Data kept under $ROOT (delete manually if desired)."
