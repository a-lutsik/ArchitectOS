# Agent Hooks SDK

ArchitectOS is already a Python package (`pip install -e .`). This is the same
memory engine the desktop app and MCP server use — one `data/architectos.db`,
no HTTP required.

```python
from architectos import ArchitectOS

mem = ArchitectOS("/path/to/data-root")   # creates data/architectos.db
mem.add("Deploy only from main.", type="Constraint")
print(mem.search("deploy from")["hits"])
pack = mem.context("how do we ship?")
mem.capture_turn("Remember: no secrets in git.", "Will redact before commit.")
```

CLI after `pip install -e .`:

```bash
architectos                         # local desktop server
architectos hook install --client all
architectos mcp                     # MCP stdio (also: architectos-mcp)
python3 -m architectos hook doctor
```

Optional extras (all auto-detected, stdlib fallback otherwise):

```bash
pip install -e ".[vectors]"      # numpy + sqlite-vec
pip install -e ".[embeddings]"   # on-device fastembed
```

`sqlite-vec` needs a Python whose SQLite was built with `load_extension`
(Homebrew, conda, most Linux distros). The python.org macOS installer is not;
ArchitectOS then keeps the in-RAM numpy / Python index and reports
`sqlite_vec.reason = load-extension-disabled`.

Settings → Memory Embeddings can check the current interpreter and, after
confirmation, install sqlite-vec into a capable Python (or Homebrew Python).
Packaged installers run the same probe (`architectos-server vector-runtime`)
during `install.sh` / `install.ps1`. sqlite-vec is baked into the freeze when
the builder Python can load SQLite extensions; pip cannot patch a frozen
bundle in place — rebuild with Homebrew/conda Python instead.

`ArchitectOS.service` is the full app service if you need something the thin
wrapper does not expose. Frozen installers expose the same `hook` subcommand
on `architectos-server` / `architectos-mcp`, so hook capture does not need
`architectos_hook.py` next to the binary.
