# waybar-pilot

AI Disclaimer: Oh yes

Intelligent waybar visibility management for Hyprland. `waybar-pilot` hides top- or bottom-positioned Waybars in autohide mode and reveals them based on cursor proximity using event-driven GTK Layer Shell sensors.

Independent waybars on each monitor; run one monitor with autohide and another monitor with a static always-shown waybar.

Unlike Waybar's built-in `mode: hide` (which requires holding a modifier key), `waybar-pilot` actively "pilots" a waybar for each monitor through different visibility states based on cursor position, fullscreen state, and workspace changes.

Based on [HideyoshiNakazone/waybar-autohide](https://github.com/HideyoshiNakazone/waybar-autohide) but rewritten with event-driven cursor detection, fullscreen awareness, hysteresis-based smart behavior, CLI interface, and robust multi-monitor support.

https://github.com/user-attachments/assets/55fc5541-eec3-4c07-b1bc-c15e5e6252a7

## Features

- **Cursor reveal**: Shows waybar when the cursor touches its configured top or bottom screen edge
- **Multi-monitor support**: Per-monitor configuration (autohide vs always-show)
- **Fullscreen awareness**: Disables cursor sensors during fullscreen
- **Workspace-aware**: Only hides waybar when fullscreen is on the active workspace
- **Event-driven**: Uses Hyprland socket2 for instant response to window changes
- **Monitor hotplug**: Handles monitor connect/disconnect automatically
- **CLI interface**: Full command-line configuration with validation

## Requirements

- Python >= 3.11
- Hyprland
- Waybar

## Installation

If `pipx` is missing, install your distro's package first.

Recommended install:

```bash
pipx install --system-site-packages git+https://github.com/nameproof/waybar-pilot.git
```

`--system-site-packages` is required so the isolated environment can access the system GTK / GI Python modules used by waybar-pilot.

Uninstall:

```bash
pipx uninstall waybar-pilot
```

Upgrade:

```bash
pipx upgrade --system-site-packages waybar-pilot
```

## Usage

Run the installed command:

```bash
waybar-pilot
```

### Recommended Usage

Find your monitor. CLI options accept `name` or `serial`:

```bash
hyprctl -j monitors
```

Add to your Hyprland config to start on launch:

```ini
exec-once = waybar-pilot --hide-monitors DP-1 --show-monitors eDP-1
```

### Command Line Options

**Action Flags:**

| Flag | Description |
|------|-------------|
| `-h, --help` | Show help message and exit |
| `-v, --version` | Show version information and exit |
| `-s, --stop` | Kill existing `waybar-pilot` and all owned waybar processes, then exit |
| `-r, --restart` | Kill existing and restart cleanly |
| `-i, --interactive` | Run in foreground with logs (default is background) |
| `--debug` | Enable debug logging |

**Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--bar-height` | Waybar height in pixels | `26` |
| `--bar-position` | Must match the effective 'position' in the main Waybar config. Multiple positions not supported (`top` or `bottom`) | `top` |
| `--hide-margin` | Extra cursor travel away from the bar before hiding | `10` |
| `--hide-monitors` | Comma-separated monitor selectors for autohide (`DP-1`, `ABC123`) | All monitors |
| `--show-monitors` | Comma-separated monitor selectors to always show (disable autohide) | None |
| `--wait-for-network` | Seconds to wait for network before starting waybar | `20` |


### Examples

```bash
# Auto-hide on DP-1, always show on eDP-1
waybar-pilot --hide-monitors DP-1 --show-monitors eDP-1

# Custom bar height with specific monitors
waybar-pilot --bar-height 30 --hide-monitors DP-1,HDMI-A-1 --show-monitors eDP-1

# Restart cleanly with custom settings
waybar-pilot -r --hide-monitors DP-1 --hide-margin 30

# Restart with debug logs
waybar-pilot -r -i --debug --hide-monitors DP-1
```

## Development

```bash
git clone https://github.com/nameproof/waybar-pilot.git
cd waybar-pilot
```

- `make check-runtime`: check detailed requirements
- `make sync`: create or update the local `uv` environment for dev tools
- `make format`: run Ruff formatting
- `make lint`: run Ruff checks
- `PYTHONPATH=src python3 -m waybar_pilot`: run the app from the source tree with system Python

## License

GPLv2
