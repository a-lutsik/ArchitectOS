#!/usr/bin/env bash
# Build the standalone ArchitectOS MCP memory server (PyInstaller one-file).
# Output: dist/mcp/architectos-mcp_<version>_<platform>.zip (+ binary + README-mcp.txt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

have_pyinstaller() {
  command -v pyinstaller >/dev/null 2>&1 && return 0
  python3 -c "import PyInstaller" >/dev/null 2>&1 && return 0
  return 1
}

if ! have_pyinstaller; then
  echo "PyInstaller is required to build architectos-mcp." >&2
  echo "  pip install 'pyinstaller>=6.0'" >&2
  echo "Then re-run: $0" >&2
  exit 1
fi

VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  *) PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac

OUT_DIR="$ROOT/dist/mcp"
WORK_DIR="$ROOT/build/mcp"
STAGE_DIR="$OUT_DIR/stage"
ZIP_NAME="architectos-mcp_${VERSION}_${PLATFORM}.zip"
BIN_NAME="architectos-mcp"

mkdir -p "$OUT_DIR"
rm -rf "$WORK_DIR" "$STAGE_DIR"
mkdir -p "$WORK_DIR" "$STAGE_DIR"

if command -v pyinstaller >/dev/null 2>&1; then
  PYI=(pyinstaller)
else
  PYI=(python3 -m PyInstaller)
fi

echo "Building architectos-mcp ${VERSION} (${PLATFORM})..."
"${PYI[@]}" \
  --noconfirm \
  --clean \
  --distpath "$OUT_DIR" \
  --workpath "$WORK_DIR" \
  "$ROOT/architectos-mcp.spec"

if [[ ! -f "$OUT_DIR/$BIN_NAME" ]]; then
  echo "Build failed: expected binary at $OUT_DIR/$BIN_NAME" >&2
  exit 1
fi

# README with Cursor / Claude config snippets (binary install path).
cat > "$OUT_DIR/README-mcp.txt" <<EOF
ArchitectOS MCP Memory Server (${VERSION})
==========================================

Standalone stdio MCP server that opens data/architectos.db directly
(no desktop app required). See docs/MCP_MEMORY_SERVER.md for full details.

Binary
------
  ${BIN_NAME}   (place somewhere on PATH, or use an absolute path below)

Shared memory with the full ArchitectOS app
-------------------------------------------
Set ARCHITECTOS_ROOT to the same root the desktop app uses (directory that
contains data/architectos.db). Defaults for this binary: ~/ArchitectOS

  export ARCHITECTOS_ROOT="\$HOME/ArchitectOS"

Quick check
-----------
  echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./${BIN_NAME}

Register in Cursor (~/.cursor/mcp.json or .cursor/mcp.json)
------------------------------------------------------------
{
  "mcpServers": {
    "architectos-memory": {
      "command": "/ABSOLUTE/PATH/TO/${BIN_NAME}",
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}

Register in Claude Desktop
--------------------------
macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
Windows: %APPDATA%\\Claude\\claude_desktop_config.json

{
  "mcpServers": {
    "architectos-memory": {
      "command": "/ABSOLUTE/PATH/TO/${BIN_NAME}",
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}

Register in Claude Code (.mcp.json or ~/.claude.json)
-----------------------------------------------------
{
  "mcpServers": {
    "architectos-memory": {
      "command": "/ABSOLUTE/PATH/TO/${BIN_NAME}",
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}

Reload the client after editing config. Tools: memory_search, memory_context,
memory_add, memory_get, memory_feedback, memory_list_projects, and related
code/memory helpers.
EOF

cp "$OUT_DIR/$BIN_NAME" "$STAGE_DIR/"
cp "$OUT_DIR/README-mcp.txt" "$STAGE_DIR/"
rm -f "$OUT_DIR/$ZIP_NAME"
(
  cd "$STAGE_DIR"
  zip -q -r "$OUT_DIR/$ZIP_NAME" "$BIN_NAME" "README-mcp.txt"
)
rm -rf "$STAGE_DIR"

echo "Wrote $OUT_DIR/$BIN_NAME"
echo "Wrote $OUT_DIR/README-mcp.txt"
echo "Wrote $OUT_DIR/$ZIP_NAME"
