BIN_PATH ?= $(HOME)/.local/bin
XDG_DATA_HOME ?= $(HOME)/.local/share
SHARE_PATH ?= $(XDG_DATA_HOME)/waybar-pilot
SCRIPT_TARGET := waybar-pilot
FORCE ?= 0


DEV_DEPENDENCIES := \
	ruff


# Check if all required binaries are installed when the Makefile is loaded

check_dev_dependencies:
	$(foreach bin,$(DEV_DEPENDENCIES),\
	  $(if $(shell command -v $(bin) 2> /dev/null),,$(error Please install `$(bin)`)))


check-runtime-deps:
	@echo "Checking runtime dependencies..."
	@command -v python3 >/dev/null 2>&1 || (echo "⚠ Warning: python3 not found" && false)
	@python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null || \
		(echo "⚠ Warning: python3-gi (PyGObject) not found" && \
		 echo "  Install with: sudo pacman -S python-gobject (Arch)" && \
		 echo "           or: sudo apt install python3-gi (Debian/Ubuntu)" && \
		 echo "           or: sudo dnf install python3-gobject (Fedora)")
	@echo "✓ Dependencies OK"


check-path:
	@# Check if BIN_PATH (expanded) is in PATH
	@BIN_PATH_EXPANDED=$$(python3 -c 'import os; import sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$(BIN_PATH)"); \
	case :$(PATH): in *:$$BIN_PATH_EXPANDED:*) ;; *) \
		echo ""; \
		echo "⚠️  Warning: $$BIN_PATH_EXPANDED is not in your PATH"; \
		echo "   You may not be able to run 'waybar-pilot' without the full path."; \
		echo ""; \
		echo "   Options:"; \
		echo "   1. Add $$BIN_PATH_EXPANDED to your PATH environment variable"; \
		echo "   2. Use a different BIN_PATH that is already in your PATH"; \
		echo "      (e.g., make install BIN_PATH=/usr/local/bin)"; \
		echo "   3. Use the full path in hyprland.conf"; \
		echo "      (e.g., exec-once = $$BIN_PATH_EXPANDED/$(SCRIPT_TARGET) --hide-monitors DP-1)"; \
		echo ""; \
		if [ "$(FORCE)" = "1" ]; then \
			echo "FORCE=1 set, continuing despite PATH warning."; \
		elif [ -t 0 ]; then \
			printf "Continue anyway? [Y/n] "; \
			read confirm; \
			case "$$confirm" in [Nn]*) echo "Installation aborted."; exit 1 ;; *) ;; esac; \
		else \
			echo "Non-interactive shell detected. Re-run with FORCE=1 to continue."; \
			exit 1; \
		fi; \
	esac


setup-dev: check_dev_dependencies
	@echo "Syncing dependencies with uv"
	@uv sync --active --script main.py


lint: check_dev_dependencies
	@echo "Linting all Python files with ruff"
	@ruff check --config ruff.toml --fix .


format: check_dev_dependencies
	@echo "Formatting all Python files with ruff"
	@ruff format --config ruff.toml .


install: check-runtime-deps check-path
	@echo "Installing executable to $(BIN_PATH)"
	@mkdir -p "$(BIN_PATH)"
	@sed 's|APP_DIR = .*|APP_DIR = Path("$(SHARE_PATH)").expanduser()|' entry_point.py > "$(BIN_PATH)/$(SCRIPT_TARGET)"
	@chmod +x "$(BIN_PATH)/$(SCRIPT_TARGET)"
	@echo "Installing modules to $(SHARE_PATH)"
	@mkdir -p "$(SHARE_PATH)"
	@install -m 644 main.py "$(SHARE_PATH)/"
	@install -m 644 config.py "$(SHARE_PATH)/"
	@install -m 644 controller.py "$(SHARE_PATH)/"
	@mkdir -p "$(SHARE_PATH)/cursor"
	@install -m 644 cursor/*.py "$(SHARE_PATH)/cursor/"
	@mkdir -p "$(SHARE_PATH)/hyprland"
	@install -m 644 hyprland/*.py "$(SHARE_PATH)/hyprland/"
	@mkdir -p "$(SHARE_PATH)/waybar"
	@install -m 644 waybar/*.py "$(SHARE_PATH)/waybar/"
	@mkdir -p "$(SHARE_PATH)/state"
	@install -m 644 state/*.py "$(SHARE_PATH)/state/"
	@echo "Installation complete!"
	@echo "  Binary: $(BIN_PATH)/$(SCRIPT_TARGET)"
	@echo "  Modules: $(SHARE_PATH)/"


uninstall:
	@echo "Removing $(SCRIPT_TARGET) from $(BIN_PATH)"
	@rm -f "$(BIN_PATH)/$(SCRIPT_TARGET)"
	@echo "Removing modules from $(SHARE_PATH)"
	@rm -rf "$(SHARE_PATH)"
	@echo "Uninstallation complete!"


.PHONY: setup-dev lint format install uninstall check_dev_dependencies check-runtime-deps check-path
