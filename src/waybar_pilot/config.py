"""Configuration management for waybar-pilot."""

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import TYPE_CHECKING, List, Set

if TYPE_CHECKING:
    from .hyprland.models import Monitor


class WaybarState(StrEnum):
    """Waybar visibility states."""

    VISIBLE = "1"
    HIDDEN = "0"


_CONNECTOR_PATTERN = re.compile(r"^(eDP|DP|HDMI|DVI|VGA|WL|LVDS|Virtual)-")


@dataclass(frozen=True)
class ResolvedMonitorSelection:
    """Resolved monitor behavior based on current monitor list."""

    autohide_ids: Set[int]
    show_ids: Set[int]
    monitor_lists_configured: bool
    unresolved_autohide: List[str]
    unresolved_show: List[str]

    def is_autohide_monitor(self, monitor_id: int) -> bool:
        """Check if a monitor should use autohide behavior."""
        if not self.monitor_lists_configured:
            return True
        return monitor_id in self.autohide_ids

    def is_show_monitor(self, monitor_id: int) -> bool:
        """Check if a monitor should always show the waybar."""
        if monitor_id in self.show_ids:
            return True
        if self.monitor_lists_configured and monitor_id not in self.autohide_ids:
            return True
        return False


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from CLI arguments."""

    # Bar dimensions
    bar_height: int
    height_threshold: int

    # Process management
    waybar_proc: str

    # Monitor configuration
    autohide_monitors: List[str]
    show_monitors: List[str]

    # Initial state
    initial_state: WaybarState

    def __post_init__(self):
        overlap = set(self.autohide_monitors) & set(self.show_monitors)
        if overlap:
            raise ValueError(f"Monitor selectors cannot overlap: {sorted(overlap)}")

    @property
    def total_detection_height(self) -> int:
        """Total height (bar + threshold) for window overlap detection."""
        return self.bar_height + self.height_threshold

    def resolve_monitor_selection(
        self,
        monitors: List["Monitor"],
    ) -> ResolvedMonitorSelection:
        """Resolve configured monitor selectors into monitor IDs."""
        monitor_lists_configured = bool(self.autohide_monitors or self.show_monitors)

        autohide_ids, unresolved_autohide = self._resolve_selector_list(
            self.autohide_monitors,
            monitors,
            list_name="hide-monitors",
        )
        show_ids, unresolved_show = self._resolve_selector_list(
            self.show_monitors,
            monitors,
            list_name="show-monitors",
        )

        overlap = autohide_ids & show_ids
        if overlap:
            raise ValueError(
                f"Resolved monitor selections overlap on monitor IDs: {sorted(overlap)}"
            )

        return ResolvedMonitorSelection(
            autohide_ids=autohide_ids,
            show_ids=show_ids,
            monitor_lists_configured=monitor_lists_configured,
            unresolved_autohide=unresolved_autohide,
            unresolved_show=unresolved_show,
        )

    def _resolve_selector_list(
        self,
        selectors: List[str],
        monitors: List["Monitor"],
        list_name: str,
    ) -> tuple[Set[int], List[str]]:
        resolved: Set[int] = set()
        unresolved: List[str] = []

        for selector in selectors:
            matches = self._resolve_selector(selector, monitors)
            if not matches:
                unresolved.append(selector)
                continue
            if len(matches) > 1:
                match_names = [m.name for m in matches]
                raise ValueError(
                    f"Ambiguous selector '{selector}' in --{list_name}: "
                    f"matches multiple monitors {match_names}"
                )
            resolved.add(matches[0].id)

        return resolved, unresolved

    def _resolve_selector(
        self, selector: str, monitors: List["Monitor"]
    ) -> List["Monitor"]:
        if _CONNECTOR_PATTERN.match(selector):
            return [m for m in monitors if m.name == selector]
        serial_matches = [m for m in monitors if (m.serial or "") == selector]
        if serial_matches:
            return serial_matches
        return [m for m in monitors if m.name == selector]

    @classmethod
    def from_args(cls, args) -> "Config":
        """Load configuration from parsed CLI arguments.

        Args:
            args: Parsed argparse.Namespace with configuration values

        Returns:
            Config instance with validated settings

        Raises:
            ValueError: If any argument is invalid
        """
        # Validate bar dimensions
        bar_height = args.bar_height
        height_threshold = args.overlap

        if bar_height <= 0:
            raise ValueError(f"bar-height must be positive, got {bar_height}")

        if height_threshold < 0:
            raise ValueError(f"overlap must be non-negative, got {height_threshold}")

        # Process name
        waybar_proc = args.procname
        if not waybar_proc or not waybar_proc.strip():
            raise ValueError("procname cannot be empty")

        # Monitor selector lists (already parsed as lists by argparse)
        autohide_monitors = args.hide_monitors if args.hide_monitors else []
        show_monitors = args.show_monitors if args.show_monitors else []

        # Initial state
        initial_state = WaybarState(args.initial_state)

        return cls(
            bar_height=bar_height,
            height_threshold=height_threshold,
            waybar_proc=waybar_proc,
            autohide_monitors=autohide_monitors,
            show_monitors=show_monitors,
            initial_state=initial_state,
        )

    def __str__(self) -> str:
        """Human-readable configuration summary."""
        return (
            f"Config(bar_height={self.bar_height}, "
            f"autohide_selectors={self.autohide_monitors}, "
            f"show_selectors={self.show_monitors})"
        )


def load_config(args) -> Config:
    """Convenience function to load configuration from CLI arguments.

    Args:
        args: Parsed argparse.Namespace with configuration values

    Returns:
        Validated Config instance

    Raises:
        ValueError: If configuration is invalid
    """
    return Config.from_args(args)
