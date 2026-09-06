"""``python -m architectos`` / the ``architectos`` console script.

Dispatches to the desktop launcher, the MCP memory server, or the hook
installer so a pip-installed package and a frozen binary share one argv shape:

    architectos                  # start the local app
    architectos hook install --client all
    architectos mcp              # MCP stdio server
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "serve"
    rest = args[1:] if args else []
    if command in {"serve", "run"}:
        from .launcher import main as launcher_main

        return launcher_main(rest)
    if command == "hook":
        from .agent_hooks import main as hook_main

        return hook_main(rest)
    if command in {"vector-runtime", "sqlite-vec"}:
        from .vec_runtime import cli_vector_runtime

        return cli_vector_runtime(rest)
    if command == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main(rest)
    if command in {"-h", "--help", "help"}:
        sys.stdout.write(
            "Usage: architectos [serve|hook|mcp|vector-runtime] ...\n"
            "  serve           Start the local desktop server (default).\n"
            "  hook            Install, uninstall, doctor, or capture agent hooks.\n"
            "  mcp             Run the MCP memory server on stdio.\n"
            "  vector-runtime  Probe sqlite-vec (optional fast memory search).\n"
        )
        return 0
    # Bare launcher flags (`--port 8766`) still start the app.
    from .launcher import main as launcher_main

    return launcher_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
