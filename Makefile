# ArchitectOS — local helper targets for installer packaging.
# Default PR CI does not run these (Tauri/PyInstaller are too heavy).

.PHONY: dist-desktop dist-mcp dist-ide dist-share dist-share-all dist-all dist-list

dist-desktop:
	@./scripts/build_desktop.sh

dist-mcp:
	@./scripts/build_mcp.sh

dist-ide:
	@./scripts/build_ide_plugin.sh

dist-share:
	@python3 scripts/build_share_package.py

dist-share-all:
	@python3 scripts/build_share_package.py --with-mcp --with-ide

dist-all:
	@python3 scripts/build_all_installers.py --target all

dist-list:
	@python3 scripts/build_all_installers.py --list
