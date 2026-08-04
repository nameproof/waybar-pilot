"""Process discovery and termination helpers for owned desktop processes."""

import os
from pathlib import Path
import signal
import time
from typing import Iterable


def _is_owned_process(pid: int) -> bool:
    """Return whether PID exists and belongs to the current user."""
    try:
        return Path(f"/proc/{pid}").stat().st_uid == os.getuid()
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _read_cmdline(pid: int) -> list[str]:
    """Read a process command line as individual arguments."""
    if not _is_owned_process(pid):
        return []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return []
    return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]


def is_waybar_pilot_process(pid: int) -> bool:
    """Return whether PID is an owned waybar-pilot process."""
    args = _read_cmdline(pid)
    if not args:
        return False

    if Path(args[0]).name == "waybar-pilot":
        return True

    if (
        len(args) > 1
        and Path(args[0]).name.startswith("python")
        and Path(args[1]).name == "waybar-pilot"
    ):
        return True

    return any(
        args[index] == "-m" and args[index + 1] == "waybar_pilot"
        for index in range(len(args) - 1)
    )


def find_waybar_pilot_pids() -> set[int]:
    """Find owned waybar-pilot processes using exact command arguments."""
    current_pid = os.getpid()
    return {
        pid
        for pid in _iter_owned_pids()
        if pid != current_pid and is_waybar_pilot_process(pid)
    }


def find_named_pids(name: str) -> set[int]:
    """Find owned processes whose Linux comm name exactly matches name."""
    matches: set[int] = set()
    for pid in _iter_owned_pids():
        try:
            process_name = Path(f"/proc/{pid}/comm").read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if process_name == name:
            matches.add(pid)
    return matches


def _iter_owned_pids() -> Iterable[int]:
    """Yield numeric /proc entries owned by the current user."""
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _is_owned_process(pid):
            yield pid


def terminate_pids(pids: Iterable[int], grace: float = 0.5) -> None:
    """Terminate PIDs gracefully, then kill processes that remain alive."""
    current_pid = os.getpid()
    remaining = {pid for pid in pids if pid > 0 and pid != current_pid}

    for pid in remaining.copy():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            remaining.discard(pid)

    deadline = time.monotonic() + grace
    while remaining and time.monotonic() < deadline:
        for pid in remaining.copy():
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                remaining.discard(pid)
        if remaining:
            time.sleep(0.05)

    for pid in remaining.copy():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            remaining.discard(pid)
