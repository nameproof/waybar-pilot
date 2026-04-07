# waybar-pilot

AI Disclaimer: Oh yes

Intelligent waybar visibility management for Hyprland. `waybar-pilot` hides waybar on window overlap and reveals it based on cursor proximity using event-driven GTK Layer Shell sensors.

Independent waybars on each monitor; run one monitor with autohide and another monitor with a static always-shown waybar.

Unlike Waybar's built-in `mode: hide` (which requires holding a modifier key), or simple auto-hide (which only responds to window overlap), `waybar-pilot` actively "pilots" a waybar for each monitor through different visibility states based on context: window overlap, cursor position, fullscreen state, and workspace changes.

Based on [HideyoshiNakazone/waybar-autohide](https://github.com/HideyoshiNakazone/waybar-autohide) but rewritten with event-driven cursor detection, fullscreen awareness, hysteresis-based smart behavior, CLI interface, and robust multi-monitor support.

https://github.com/user-attachments/assets/55fc5541-eec3-4c07-b1bc-c15e5e6252a7

## Features

- **Auto-hide on overlap**: Automatically hides waybar when a window overlaps with the bar area
- **Cursor reveal**: Shows waybar when the cursor touches the top edge of the screen using event-driven sensors
- **Multi-monitor support**: Per-monitor configuration (autohide vs always-show)
- **Fullscreen awareness**: Disables cursor sensors during fullscreen
- **Hysteresis**: Different thresholds for showing vs keeping visibility to prevent flicker
- **Workspace-aware**: Only hides waybar when fullscreen is on the active workspace
- **Event-driven**: Uses Hyprland socket2 for instant response to window changes
- **Monitor hotplug**: Handles monitor connect/disconnect automatically
- **CLI interface**: Full command-line configuration with validation

## Requirements

- Python >= 3.14
- Hyprland
- `hyprctl`
- Waybar
- Python GI bindings with GTK 3
- `GtkLayerShell` GI typelib

## Runtime Dependencies

Most of the app is standard-library Python, but runtime still depends on system-provided desktop pieces:

- `hyprctl` for Hyprland state and cursor queries
- `waybar` for the managed bars
- `gi` / PyGObject so Python can import GTK
- `GtkLayerShell` so the top-edge reveal sensors can exist

That matters because these are usually installed by your distro, not by Python package metadata.

You can verify the current machine with:

```bash
make check-runtime
```

## Installation

This project now uses normal Python packaging via `pyproject.toml`.

Recommended install for normal use:

```bash
git clone https://github.com/nameproof/waybar-pilot.git
cd waybar-pilot

# Install as an isolated CLI app
pipx install .
```

Alternative with plain pip:

```bash
python3 -m pip install --user .
```

If `pipx` or `python3 -m pip` is missing, install your distro's package first.

Uninstall:

```bash
pipx uninstall waybar-pilot

# Or if you used pip instead
python3 -m pip uninstall waybar-pilot
```

## Development

Development tooling is now real instead of placeholder targets:

- `make sync`: create or update the local `uv` environment for dev tools
- `make lint`: run Ruff checks
- `make format`: run Ruff formatting
- `make run`: run the app from the source tree with system Python

Installation is intentionally documented as direct `pipx` / `pip` commands instead of wrapping those commands in `make`.

Setup:

```bash
make sync
```

Typical workflow:

```bash
make lint
make format
make run
```

`make run` intentionally uses system Python with `PYTHONPATH=src` instead of the `uv` environment. On many Linux systems, `gi` and `GtkLayerShell` are available to system Python but not to an isolated virtualenv.

## Usage

Run the installed command:

```bash
waybar-pilot
```

Or run directly from the checkout while developing:

```bash
make run
```

### Recommended Usage

Find your monitor. CLI options accept `name` or `serial`:

```bash
hyprctl -j monitors
```

Add to your Hyprland config to start on launch. The sleep gives the original waybar time to start so `waybar-pilot` can replace it cleanly if needed:

```ini
exec-once = sleep 2 && waybar-pilot --hide-monitors DP-1 --show-monitors eDP-1
```

### Command Line Options

**Action Flags:**

| Flag | Description |
|------|-------------|
| `-h, --help` | Show help message and exit |
| `-s, --stop` | Kill existing `waybar-pilot` and managed waybar processes, then exit |
| `-r, --restart` | Kill existing and restart cleanly |
| `-i, --interactive` | Run in foreground with logs (default is background) |
| `--debug` | Enable debug logging |

**Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--bar-height` | Waybar height in pixels | `26` |
| `--overlap` | Extra pixels below the bar used for overlap and leave detection | `10` |
| `--procname` | Process name to manage | `waybar` |
| `--hide-monitors` | Comma-separated monitor selectors for autohide (`DP-1`, `ABC123`) | All monitors |
| `--show-monitors` | Comma-separated monitor selectors to always show (disable autohide) | None |
| `--initial-state` | Initial state for hide-monitors: `0`=hidden or `1`=visible | `0` |

Reveal notes:
`CursorSensor.TRIGGER_HEIGHT` is currently hardcoded to `1`, so showing only triggers at the top edge even though the physical sensor remains taller for stable leave detection.

### Examples

```bash
# Stop all running waybar-pilot and managed waybar processes
waybar-pilot -s

# Auto-hide on DP-1, always show on eDP-1
waybar-pilot --hide-monitors DP-1 --show-monitors eDP-1

# Increase overlap detection zone for better window detection
waybar-pilot --overlap 40

# Custom bar height with specific monitors
waybar-pilot --bar-height 30 --hide-monitors DP-1,HDMI-A-1 --show-monitors eDP-1

# Restart cleanly with custom settings
waybar-pilot -r --hide-monitors DP-1 --overlap 30

# Restart and see logs for debugging
waybar-pilot -r -i --hide-monitors DP-1,HDMI-A-1

# Restart with debug logs
waybar-pilot -r -i --debug --hide-monitors DP-1
```

## Project Layout

```text
src/waybar_pilot/
```

The code now lives in a normal Python package instead of a copied share directory plus a custom launcher.

## License

GPLv2
