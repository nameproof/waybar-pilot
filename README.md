# waybar-pilot

AI Disclaimer: Oh yes

Intelligent waybar visibility management for Hyprland. Hides waybar on window overlap and reveals it based on cursor proximity using event-driven GTK Layer Shell sensors.

Independent waybars on each monitor; run one monitor with autohide and another monitor with a static always shown waybar.

Unlike Waybar's built-in `mode: hide` (which requires holding a modifier key), or simple auto-hide (which only responds to window overlap), waybar-pilot actively "pilots" a waybar for each monitor through different visibility states based on context: window overlap, cursor position, fullscreen state, and workspace changes.

Based on [HideyoshiNakazone/waybar-autohide](https://github.com/HideyoshiNakazone/waybar-autohide) but rewritten with event-driven cursor detection, fullscreen awareness, hysteresis-based smart behavior, CLI interface, and robust multi-monitor support.

https://github.com/user-attachments/assets/55fc5541-eec3-4c07-b1bc-c15e5e6252a7

## Features

- **Auto-hide on overlap**: Automatically hides waybar when a window overlaps with the bar area
- **Cursor reveal**: Shows waybar when the cursor touches the top edge of the screen using event-driven sensors
- **Multi-monitor support**: Per-monitor configuration (autohide vs always-show)
- **Fullscreen awareness**: Disables cursor sensors during fullscreen
- **Hysteresis**: Different thresholds for showing (hard) vs keeping (easy) - prevents flicker
- **Workspace-aware**: Only hides waybar when fullscreen is on the active workspace
- **Event-driven**: Uses Hyprland socket2 for instant response to window changes
- **Monitor hotplug**: Handles monitor connect/disconnect automatically
- **CLI interface**: Full command-line configuration with validation

## Requirements

- Python >= 3.14
- Hyprland window manager
- Waybar

## Installation

It's just a python script broken up for some structure.
- `BIN_PATH` - Executable entry point
- `SHARE_PATH` - Python modules and packages

```bash
# Download zip
curl -L -O https://github.com/nameproof/waybar-pilot/archive/refs/heads/main.zip
unzip main.zip

# Or git clone
git clone https://github.com/nameproof/waybar-pilot.git

# Install to ~/.local/bin and ${XDG_DATA_HOME}/waybar-pilot
# (or ~/.local/share/waybar-pilot if XDG_DATA_HOME is unset)
make install

# Or specify custom paths
make install BIN_PATH=/usr/local/bin SHARE_PATH=/usr/local/share/waybar-pilot

# Uninstall
make uninstall
```

## Usage

Simply run the script

```bash
waybar-pilot
```

### Recommended Usage

Find your monitor. CLI options accept 'name' or 'serial':

```bash
hyprctl -j monitors
```

Add to your Hyprland config to start on launch (sleep to make sure original waybar has started and can be replaced if needed):

```
exec-once = sleep 2 && waybar-pilot --hide-monitors DP-1 --show-monitors eDP-1
```

### Command Line Options

**Action Flags:**

| Flag | Description |
|------|-------------|
| `-h, --help` | Show help message and exit |
| `-s, --stop` | Kill existing waybar-pilot and managed waybar processes, then exit |
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

## License

GPLv2
