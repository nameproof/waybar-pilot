PYTHON ?= python3
UV ?= uv

.PHONY: sync lint format check-runtime

sync:
	@command -v $(UV) >/dev/null 2>&1 || { \
		echo "uv is required for development sync."; \
		echo "Install it from: https://docs.astral.sh/uv/getting-started/installation/"; \
		exit 1; \
	}
	@$(UV) sync

lint:
	@command -v $(UV) >/dev/null 2>&1 || { \
		echo "uv is required to run ruff."; \
		echo "Run 'make sync' after installing uv."; \
		exit 1; \
	}
	@$(UV) run ruff check .

format:
	@command -v $(UV) >/dev/null 2>&1 || { \
		echo "uv is required to run ruff."; \
		echo "Run 'make sync' after installing uv."; \
		exit 1; \
	}
	@$(UV) run ruff format .

check-runtime:
	@command -v hyprctl >/dev/null 2>&1 || { \
		echo "Missing runtime dependency: hyprctl"; \
		exit 1; \
	}
	@command -v waybar >/dev/null 2>&1 || { \
		echo "Missing runtime dependency: waybar"; \
		exit 1; \
	}
	@$(PYTHON) -c "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('GtkLayerShell', '0.1'); from gi.repository import Gtk, GtkLayerShell" >/dev/null 2>&1 || { \
		echo "Missing Python GI runtime pieces (Gtk 3 / GtkLayerShell)."; \
		echo "Install the system packages that provide python gi bindings and gtk-layer-shell typelibs."; \
		exit 1; \
	}
	@echo "Runtime dependencies look available."
