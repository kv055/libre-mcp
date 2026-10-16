"""Display-environment recovery so live mode survives sanitized MCP hosts.

Hosts spawn the server with a stripped environment (no WAYLAND_DISPLAY,
DISPLAY, or compositor variables), which leaves a "visible" soffice with no
display to connect to — live mode then silently shows nothing. Before
launching soffice, recover the display from the running desktop session:
keep an explicitly configured display, else rediscover the Wayland socket
(plus the Hyprland instance id where present) from the runtime directory.

Multi-session caveat: with several live Wayland sockets, rediscovery picks
the first in sorted order, which may not be your session — set
WAYLAND_DISPLAY explicitly in that case.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from typing import MutableMapping


@dataclass(frozen=True)
class DisplayInfo:
    kind: str  # "wayland" | "x11" | "native" | "none"
    detail: str  # socket name / DISPLAY value / platform / no-display reason


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _wayland_socket(runtime: str) -> str | None:
    """Basename of a live Wayland socket; first in sorted order."""
    try:
        names = sorted(os.listdir(runtime))
    except OSError:
        return None
    for name in names:
        if name.startswith("wayland-") and _is_socket(os.path.join(runtime, name)):
            return name
    return None


def _hyprland_signature(runtime: str) -> str | None:
    """Newest Hyprland instance id (stale crash dirs lose)."""
    hypr = os.path.join(runtime, "hypr")
    try:
        entries = [
            (os.path.getmtime(os.path.join(hypr, entry)), entry)
            for entry in os.listdir(hypr)
            if os.path.isdir(os.path.join(hypr, entry))
        ]
    except OSError:
        return None
    if not entries:
        return None
    return max(entries)[1]


def ensure_display(
    env: MutableMapping[str, str] | None = None,
    runtime_dir: str | None = None,
) -> DisplayInfo:
    """Ensure `env` carries working display variables for a visible soffice.

    Mutates `env` in place (default: the process environment) and reports
    what the display will be. A configured-but-stale WAYLAND_DISPLAY is
    replaced by rediscovery; an explicit DISPLAY is trusted as-is.
    """
    if env is None:
        env = os.environ
    if sys.platform == "darwin":
        # Aqua needs no display variables; nothing to recover or validate.
        return DisplayInfo("native", "macOS (no display variables needed)")
    runtime = (
        runtime_dir
        or env.get("XDG_RUNTIME_DIR")
        or os.getenv("XDG_RUNTIME_DIR")
        or f"/run/user/{os.geteuid()}"
    )

    current = env.get("WAYLAND_DISPLAY")
    if current:
        sock = current if os.path.isabs(current) else os.path.join(runtime, current)
        if _is_socket(sock):
            return DisplayInfo("wayland", current)
        # Configured but stale: fall through and try to rediscover.
    if env.get("DISPLAY"):
        return DisplayInfo("x11", env["DISPLAY"])

    found = _wayland_socket(runtime)
    if found is None:
        return DisplayInfo("none", f"no Wayland socket under {runtime}")
    env["WAYLAND_DISPLAY"] = found
    env["XDG_RUNTIME_DIR"] = runtime
    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE") or _hyprland_signature(runtime)
    if signature:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
    return DisplayInfo("wayland", found)
