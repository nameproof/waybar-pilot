"""Waybar autohide - entry point."""

import argparse
import atexit
import faulthandler
from importlib.metadata import PackageNotFoundError, version
import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib

from .config import WAYBAR_PROC
from .processes import (
    find_named_pids,
    find_waybar_pilot_pids,
    is_waybar_pilot_process,
    terminate_pids,
)

# Marker set on the real background child so it runs the app directly
# instead of trying to daemonize again.
BACKGROUND_ENV = "WAYBAR_PILOT_BACKGROUND"
LOG_FORMAT = "%(asctime)s %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
_CRASH_AIDS_INSTALLED = False


class _HelpFormatter(argparse.HelpFormatter):
    """Place usage below its heading without affecting line alignment."""

    def _format_usage(self, usage, actions, groups, prefix):
        formatted = super()._format_usage(usage, actions, groups, prefix="  ")
        heading = f"{self._theme.heading}usage:{self._theme.reset}"
        return f"{heading}\n{formatted}"


class _HelpArgumentParser(argparse.ArgumentParser):
    """Render description first and guidance immediately before options."""

    def __init__(self, *args, options_intro: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._options_intro = options_intro

    def format_help(self) -> str:
        formatter = self._get_formatter()
        formatter.add_text(self.description)
        formatter.add_usage(
            self.usage,
            self._actions,
            self._mutually_exclusive_groups,
        )
        formatter.add_text(self._options_intro)
        for action_group in self._action_groups:
            formatter.start_section(action_group.title)
            formatter.add_text(action_group.description)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _get_version() -> str:
    """Read the source-tree version, then fall back to installed metadata."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            with pyproject_path.open("rb") as f:
                data = tomllib.load(f)
            return data["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            pass

    try:
        return version("waybar-pilot")
    except PackageNotFoundError:
        return "unknown"


class _Spinner:
    """Minimal terminal spinner, no dependencies."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _INTERVAL = 0.08

    def __init__(self, message: str):
        self._message = message
        self._running = False
        self._thread: threading.Thread | None = None
        self._frame_idx = 0

    def start(self) -> None:
        if not sys.stdout.isatty():
            print(self._message, flush=True)
            return
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, message: str) -> None:
        self._message = message

    def _spin(self) -> None:
        while self._running:
            frame = self._FRAMES[self._frame_idx % len(self._FRAMES)]
            sys.stdout.write(f"\r{frame} {self._message}")
            sys.stdout.flush()
            self._frame_idx += 1
            time.sleep(self._INTERVAL)

    def stop(self, final: str | None = None) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        if sys.stdout.isatty():
            width = len(self._message) + 3
            sys.stdout.write("\r" + " " * width + "\r")
            if final:
                sys.stdout.write(final + "\n")
            sys.stdout.flush()
        else:
            if final:
                print(final, flush=True)


def _get_runtime_dir() -> Path:
    """Return the runtime state directory for waybar-pilot."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "waybar-pilot"

    return Path("/tmp") / f"waybar-pilot-{os.getuid()}"


def _get_runtime_log_path() -> Path:
    """Return the log path for background runs."""
    return _get_runtime_dir() / "waybar-pilot.log"


def _ensure_runtime_dir() -> Path:
    """Create and validate the private runtime directory."""
    runtime_dir = _get_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    info = runtime_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(
            f"Unsafe runtime directory {runtime_dir}: expected an owned real directory"
        )

    if stat.S_IMODE(info.st_mode) != 0o700:
        runtime_dir.chmod(0o700)
    return runtime_dir


def _open_runtime_dir() -> int:
    """Open the validated runtime directory without following a final symlink."""
    runtime_dir = _ensure_runtime_dir()
    return os.open(
        runtime_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


def _open_runtime_file(name: str, flags: int) -> int:
    """Open an owned regular runtime file without following symlinks."""
    directory_fd = _open_runtime_dir()
    try:
        file_fd = os.open(
            name,
            flags | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)

    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"Unsafe runtime file: {name}")
        os.fchmod(file_fd, 0o600)
        return file_fd
    except Exception:
        os.close(file_fd)
        raise


def _read_pid_file() -> int | None:
    """Read PID from file if it exists and is valid."""
    try:
        file_fd = _open_runtime_file("waybar-pilot.pid", os.O_RDONLY)
        with os.fdopen(file_fd, encoding="utf-8") as pid_file:
            return int(pid_file.read(64).strip())
    except (FileNotFoundError, ValueError, OSError, RuntimeError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still alive."""
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but is owned by another user
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_pid_file() -> None:
    """Write current PID to the PID file."""
    file_fd = _open_runtime_file(
        "waybar-pilot.pid",
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    )
    with os.fdopen(file_fd, "w", encoding="utf-8") as pid_file:
        pid_file.write(str(os.getpid()))


def _remove_pid_file() -> None:
    """Remove the PID file if it exists."""
    try:
        directory_fd = _open_runtime_dir()
    except (OSError, RuntimeError):
        return
    try:
        os.unlink("waybar-pilot.pid", dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(directory_fd)


def _kill_by_pid_file() -> bool:
    """Kill the process referenced by the PID file.

    Returns True if a process was found and signaled, False otherwise.
    """
    pid = _read_pid_file()
    if pid is None:
        return False

    if not _is_pid_alive(pid):
        _remove_pid_file()
        return False

    if not is_waybar_pilot_process(pid):
        _remove_pid_file()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        _remove_pid_file()
        return False

    # Wait up to 2 seconds for graceful shutdown
    for _ in range(20):
        time.sleep(0.1)
        if not _is_pid_alive(pid):
            _remove_pid_file()
            return True

    # Escalate to SIGKILL
    log = logging.getLogger("waybar-pilot")
    log.debug("Escalating to SIGKILL for pid=%s", pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    time.sleep(0.3)
    if not _is_pid_alive(pid):
        _remove_pid_file()
        return True

    return False


def _configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure timestamped logging for interactive and background runs."""
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format=LOG_FORMAT,
            datefmt=LOG_DATE_FORMAT,
            stream=sys.stderr,
        )
    else:
        root_logger.setLevel(level)

    logger = logging.getLogger("waybar-pilot")
    logger.setLevel(level)
    return logger


def _install_crash_aids() -> None:
    """Install traceback and crash diagnostics for background runs."""
    global _CRASH_AIDS_INSTALLED

    if _CRASH_AIDS_INSTALLED:
        return

    log = logging.getLogger("waybar-pilot")

    def _log_unhandled_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical(
            "Unhandled top-level exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is KeyboardInterrupt:
            return
        thread_name = args.thread.name if args.thread else "unknown"
        log.critical(
            "Unhandled exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    except Exception:
        log.exception("Failed to enable faulthandler diagnostics")
    else:
        log.debug("Crash diagnostics enabled")

    sys.excepthook = _log_unhandled_exception
    threading.excepthook = _log_thread_exception
    atexit.register(lambda: log.info("waybar-pilot process exiting"))

    _CRASH_AIDS_INSTALLED = True


def check_requirements() -> bool:
    """Check that required dependencies are available.

    Returns:
        True if all requirements met, False otherwise
    """
    log = logging.getLogger("waybar-pilot")

    waybar_path = shutil.which(WAYBAR_PROC)
    if waybar_path is None:
        log.error("Missing required dependency - waybar not found in PATH")
        return False

    try:
        result = subprocess.run(
            [WAYBAR_PROC, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.error("Failed to run %s --version: %s", WAYBAR_PROC, e)
        return False

    version_output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0 or "waybar" not in version_output:
        log.error("%s --version did not return expected Waybar output", WAYBAR_PROC)
        return False

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import Gtk as _Gtk  # noqa: F401
        from gi.repository import GtkLayerShell as _GtkLayerShell  # noqa: F401

        return True
    except (ImportError, ValueError):
        log.error("Missing required dependency - GTK 3 or GtkLayerShell bindings")
        log.error("Please install the required PyGObject and layer-shell packages:")
        log.error("  Arch: sudo pacman -S python-gobject")
        log.error("  Debian/Ubuntu: sudo apt install python3-gi")
        log.error("  Fedora: sudo dnf install python3-gobject")
        return False
    except Exception as e:
        log.exception("Failed to initialize GTK: %s", e)
        return False


def _kill_existing_processes() -> None:
    """Kill existing waybar-pilot instances and managed bar processes."""
    # Prefer PID file for clean shutdown
    _kill_by_pid_file()

    # With no live controller, every owned waybar is unmanaged.
    terminate_pids(find_named_pids(WAYBAR_PROC))

    # Recover from a missing or stale PID file without broad pattern matching.
    terminate_pids(find_waybar_pilot_pids())


def _build_module_command(args) -> list[str]:
    """Build Python module command preserving current interpreter/env."""
    cmd = [sys.executable, "-m", "waybar_pilot"]

    if args.bar_height != 26:
        cmd.extend(["--bar-height", str(args.bar_height)])
    if args.bar_position != "top":
        cmd.extend(["--bar-position", args.bar_position])
    if args.hide_margin != 10:
        cmd.extend(["--hide-margin", str(args.hide_margin)])
    if args.hide_monitors:
        cmd.extend(["--hide-monitors", ",".join(map(str, args.hide_monitors))])
    if args.show_monitors:
        cmd.extend(["--show-monitors", ",".join(map(str, args.show_monitors))])
    if getattr(args, "wait_for_network", 20) != 20:
        cmd.extend(["--wait-for-network", str(args.wait_for_network)])
    if args.debug:
        cmd.append("--debug")

    return cmd


def stop_and_exit() -> int:
    """Kill any existing waybar-pilot and managed bar processes, then exit."""
    spinner = _Spinner("Stopping waybar-pilot...")
    spinner.start()

    if _kill_by_pid_file():
        spinner.stop("Stopped.")
        return 0

    # Fallback to old behavior
    _kill_existing_processes()
    spinner.stop("Stopped.")
    return 0


def _run_detached(args) -> int:
    """Launch waybar-pilot in background (returns immediately)."""
    cmd = _build_module_command(args)
    env = os.environ.copy()
    env[BACKGROUND_ENV] = "1"

    log_fd = _open_runtime_file(
        "waybar-pilot.log",
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    )
    with os.fdopen(log_fd, "w", encoding="utf-8") as log_file:
        subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    return 0


def restart_and_run(args, interactive: bool = False) -> int:
    """Kill any existing waybar-pilot and managed bar processes, then run normally.

    Args:
        args: Parsed command line arguments
        interactive: If False, launch in background. If True, stay in foreground.

    Returns:
        Exit code from main() (or 0 if launched to background)
    """
    spinner = _Spinner("Restarting waybar-pilot...")
    spinner.start()
    _kill_existing_processes()

    spinner.update("Starting waybar-pilot...")
    if interactive:
        spinner.stop("Started waybar-pilot.")
        print("Running in interactive mode (Ctrl+C to stop)")
        return _run_main(args)
    _run_detached(args)
    time.sleep(0.5)
    log_path = _get_runtime_log_path()
    spinner.stop(f"Started waybar-pilot (log: {log_path})")
    return 0


def _parse_monitor_list(value):
    """Parse comma-separated monitor selector list."""
    if not value:
        return []
    selectors = [x.strip() for x in value.split(",") if x.strip()]
    if not selectors:
        raise argparse.ArgumentTypeError(
            f"Invalid monitor list: {value}. Expected comma-separated monitor selectors."
        )
    return selectors


def _positive_int(value):
    """Validate positive integer."""
    try:
        ivalue = int(value)
        if ivalue <= 0:
            raise argparse.ArgumentTypeError(f"{value} must be a positive integer")
        return ivalue
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value} is not a valid integer")


def _non_negative_int(value):
    """Validate non-negative integer."""
    try:
        ivalue = int(value)
        if ivalue < 0:
            raise argparse.ArgumentTypeError(f"{value} must be a non-negative integer")
        return ivalue
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value} is not a valid integer")


def _run_main(args) -> int:
    """Run the main application logic.

    Args:
        args: Parsed command line arguments
    """
    log_level = logging.DEBUG if args.debug else logging.INFO
    log = _configure_logging(log_level)
    _install_crash_aids()

    # Implicit restart: kill any live instance from PID file
    current_pid = os.getpid()
    existing_pid = _read_pid_file()
    if existing_pid is not None and existing_pid != current_pid:
        if _is_pid_alive(existing_pid) and is_waybar_pilot_process(existing_pid):
            log.info(
                "Implicit restart: stopping existing instance (pid=%s)", existing_pid
            )
            _kill_by_pid_file()
        else:
            _remove_pid_file()

    # Register PID file and cleanup
    _write_pid_file()
    atexit.register(_remove_pid_file)

    # Check requirements first
    if not check_requirements():
        return 1

    try:
        from .config import load_config
        from .controller import AutohideController

        # Load configuration from CLI arguments
        config = load_config(args)

        # Create and run controller
        controller = AutohideController(config)

        if controller.initialize():
            controller.run()
            return 0
        else:
            return 1

    except ValueError as e:
        log.error("Configuration error: %s", e)
        return 1
    except Exception as e:
        log.exception("Fatal error: %s", e)
        return 1


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = _HelpArgumentParser(
        description=(
            "Per-monitor Waybar manager with hide and show functionality based on "
            "cursor position."
        ),
        options_intro=(
            "Monitor selectors accept the name or serial reported by "
            "`hyprctl -j monitors`."
        ),
        formatter_class=_HelpFormatter,
    )

    # Action flags (short forms)
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "-r",
        "--restart",
        action="store_true",
        help="Kill any existing waybar-pilot and waybar processes, then start fresh",
    )
    action_group.add_argument(
        "-s",
        "--stop",
        action="store_true",
        help="Kill any existing waybar-pilot and waybar processes, then exit",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"waybar-pilot {_get_version()}",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Run in foreground with log output (default runs in background)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    # Configuration options (long forms only)
    parser.add_argument(
        "--bar-height",
        type=_positive_int,
        default=26,
        help="Waybar height in pixels (default: 26)",
    )
    parser.add_argument(
        "--bar-position",
        choices=("top", "bottom"),
        default="top",
        help=(
            "Must match the effective 'position' in the main Waybar config. "
            "Multiple positions not supported (default: top)"
        ),
    )
    parser.add_argument(
        "--hide-margin",
        type=_non_negative_int,
        default=10,
        help="Extra cursor travel away from the bar before hiding (default: 10)",
    )
    parser.add_argument(
        "--hide-monitors",
        type=_parse_monitor_list,
        default=[],
        help=(
            "Comma-separated monitor selectors with autohide behavior "
            '(monitor name like "DP-1" or monitor serial like "ABC123", default: all monitors)'
        ),
    )
    parser.add_argument(
        "--show-monitors",
        type=_parse_monitor_list,
        default=[],
        help=(
            "Comma-separated monitor selectors always visible "
            '(monitor name like "HDMI-A-1" or serial like "XYZ987", default: none)'
        ),
    )
    parser.add_argument(
        "--wait-for-network",
        type=_non_negative_int,
        default=20,
        help="Seconds to wait for network before starting waybar (default: 20)",
    )

    args = parser.parse_args()
    is_background = os.environ.get(BACKGROUND_ENV) == "1"

    if args.stop:
        return stop_and_exit()
    if args.restart:
        return restart_and_run(args, interactive=args.interactive)
    if args.interactive or is_background:
        return _run_main(args)
    _run_detached(args)
    print(f"Started waybar-pilot (log: {_get_runtime_log_path()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
