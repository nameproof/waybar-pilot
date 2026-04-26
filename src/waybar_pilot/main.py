"""Waybar autohide - entry point."""

import atexit
import argparse
import faulthandler
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

DETACHED_CHILD_ENV = "WAYBAR_PILOT_DETACHED_CHILD"
DETACHED_WRAPPER_ENV = "WAYBAR_PILOT_DETACHED_WRAPPER"
LOG_FORMAT = "%(asctime)s %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
_CRASH_AIDS_INSTALLED = False


def _get_runtime_log_path() -> Path:
    """Return the log path for detached/background runs."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "waybar-pilot.log"

    return Path("/tmp") / f"waybar-pilot-{os.getuid()}.log"


def _configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure timestamped logging for both interactive and detached runs."""
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

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk as _Gtk  # noqa: F401

        return True
    except ImportError:
        log.error("Missing required dependency - PyGObject (GTK bindings)")
        log.error("Please install python-gobject:")
        log.error("  Arch: sudo pacman -S python-gobject")
        log.error("  Debian/Ubuntu: sudo apt install python3-gi")
        log.error("  Fedora: sudo dnf install python3-gobject")
        return False
    except Exception as e:
        log.exception("Failed to initialize GTK: %s", e)
        return False


def _kill_existing_processes(args) -> None:
    """Kill existing waybar-pilot instances and managed bar processes.

    Args:
        args: Parsed command line arguments
    """
    # Keep managed process name consistent with --procname.
    procname = (
        args.procname.strip() if args.procname and args.procname.strip() else "waybar"
    )

    current_pid = os.getpid()

    # First kill all managed bar processes (the children)
    # This must be done BEFORE killing waybar-pilot parent
    subprocess.run(["pkill", "-9", "-x", procname], capture_output=True)

    # Kill waybar-pilot processes but NOT ourselves
    # Use pgrep to find PIDs, then kill excluding current_pid
    try:
        result = subprocess.run(
            ["pgrep", "-x", "waybar-pilot"], capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    try:
                        pid = int(line.strip())
                        if pid != current_pid:
                            os.kill(pid, 9)
                    except (ValueError, ProcessLookupError):
                        pass
    except Exception:
        pass

    # Also try to kill any other python processes running waybar-pilot
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*waybar-pilot"], capture_output=True, text=True
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    try:
                        pid = int(line.strip())
                        if pid != current_pid:
                            os.kill(pid, 9)
                    except (ValueError, ProcessLookupError):
                        pass
    except Exception:
        pass

    # Wait for processes to die
    time.sleep(0.5)


def _build_module_command(args) -> list[str]:
    """Build Python module command preserving current interpreter/env."""
    cmd = [sys.executable, "-m", "waybar_pilot"]

    if args.bar_height != 26:
        cmd.extend(["--bar-height", str(args.bar_height)])
    if args.overlap != 10:
        cmd.extend(["--overlap", str(args.overlap)])
    if args.procname != "waybar":
        cmd.extend(["--procname", args.procname])
    if args.hide_monitors:
        cmd.extend(["--hide-monitors", ",".join(map(str, args.hide_monitors))])
    if args.show_monitors:
        cmd.extend(["--show-monitors", ",".join(map(str, args.show_monitors))])
    if args.initial_state != "0":
        cmd.extend(["--initial-state", args.initial_state])
    if args.debug:
        cmd.append("--debug")

    return cmd


def stop_and_exit(args) -> int:
    """Kill any existing waybar-pilot and managed bar processes, then exit."""
    print("Stopping waybar-pilot and managed bar processes...")
    _kill_existing_processes(args)
    print("Stopped.")
    return 0


def _build_detached_command(args) -> list[str]:
    """Build detached wrapper command."""
    return _build_module_command(args)


def _run_detached_wrapper(args) -> int:
    """Run detached wrapper that supervises actual app process."""
    log_level = logging.DEBUG if args.debug else logging.INFO
    log = _configure_logging(log_level)
    _install_crash_aids()

    child_env = os.environ.copy()
    child_env.pop(DETACHED_WRAPPER_ENV, None)
    child_env[DETACHED_CHILD_ENV] = "1"
    child_cmd = _build_module_command(args)

    log.info(
        "Detached wrapper starting child: pid=%s ppid=%s session=%s",
        os.getpid(),
        os.getppid(),
        os.getsid(0),
    )
    log.debug("Detached child command: %s", child_cmd)

    child = subprocess.Popen(child_cmd, env=child_env)
    return_code = child.wait()

    if return_code < 0:
        try:
            signal_name = signal.Signals(-return_code).name
        except ValueError:
            signal_name = f"SIG{-return_code}"
        log.critical(
            "Detached child exited from signal %s (%s)",
            signal_name,
            -return_code,
        )
    else:
        log.warning("Detached child exited with code %s", return_code)

    return return_code


def _run_detached(args) -> int:
    """Launch waybar-pilot in background and return immediately."""
    cmd = _build_detached_command(args)
    env = os.environ.copy()
    env.pop(DETACHED_CHILD_ENV, None)
    env[DETACHED_WRAPPER_ENV] = "1"
    log_path = _get_runtime_log_path()

    print(f"Starting waybar-pilot in background (log: {log_path})...")

    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as log_file:
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
        interactive: If False, detach to background. If True, stay in foreground.

    Returns:
        Exit code from main() (or 0 if detached to background)
    """
    print("Restarting waybar-pilot...")
    _kill_existing_processes(args)

    if interactive:
        # Stay in foreground, show logs
        print("Running in interactive mode (Ctrl+C to stop)")
        return _run_main(args)
    else:
        return _run_detached(args)


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


def _initial_state(value):
    """Validate initial state (0 or 1)."""
    if value not in ("0", "1"):
        raise argparse.ArgumentTypeError(f"{value} must be 0 or 1")
    return value


def _run_main(args) -> int:
    """Run the main application logic.

    Args:
        args: Parsed command line arguments
    """
    log_level = logging.DEBUG if args.debug else logging.INFO
    log = _configure_logging(log_level)
    _install_crash_aids()

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
    parser = argparse.ArgumentParser(
        description="waybar-pilot - automatically hide/show waybar based on cursor position",
        epilog="""
Examples:
  waybar-pilot                             Start with defaults
  waybar-pilot -i                          Run in foreground with logs
  waybar-pilot -s                          Stop existing waybar-pilot/waybar
  waybar-pilot --bar-height 30             Custom bar height
  waybar-pilot --hide-monitors DP-1,eDP-1  Autohide on selected monitors
  waybar-pilot -r                          Restart cleanly
  waybar-pilot -r -i                       Restart with logs
  waybar-pilot -h                          Show help
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--overlap",
        type=_non_negative_int,
        default=10,
        help="Extra pixels below the bar used for overlap and leave detection (default: 10)",
    )
    parser.add_argument(
        "--procname",
        type=str,
        default="waybar",
        help="Process name to manage (default: waybar)",
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
        "--initial-state",
        type=_initial_state,
        default="0",
        help="Initial state: 0=hidden, 1=visible (default: 0)",
    )

    args = parser.parse_args()
    detached_child = os.environ.get(DETACHED_CHILD_ENV) == "1"
    detached_wrapper = os.environ.get(DETACHED_WRAPPER_ENV) == "1"

    if args.stop:
        return stop_and_exit(args)
    if detached_wrapper:
        return _run_detached_wrapper(args)
    if args.restart:
        return restart_and_run(args, interactive=args.interactive)
    if args.interactive or detached_child:
        return _run_main(args)
    return _run_detached(args)


if __name__ == "__main__":
    sys.exit(main())
