"""beta1.0.84: C++ module bridge (netops / ics).

One thin client for the two C++ utility exes. Python keeps ZERO native
logic here — each helper spawns the C++ module, captures UTF-8 stdout, and
returns parsed values. If an exe is missing (old package) the caller falls
back to its legacy path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

def _exe(name: str) -> str:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "engine", name)


def _run(name: str, args, timeout=25) -> str:
    """Run a C++ module; returns stdout text (UTF-8). Raises OSError."""
    return subprocess.run(
        [_exe(name), *args], capture_output=True, timeout=timeout,
    ).stdout.decode("utf-8", "replace")


def available(name: str) -> bool:
    return os.path.isfile(_exe(name))


# ---- netops ----

def netops_available() -> bool:
    return available("linkhelm_netops.exe")


def show_interfaces() -> str:
    return _run("linkhelm_netops.exe", ["SHOW_INTERFACES"])


def if_index(name: str):
    """Interface index for an adapter name, or None."""
    try:
        out = _run("linkhelm_netops.exe", ["IF_INDEX", name])
        return int(out.strip())
    except (ValueError, OSError):
        return None


def set_address(name: str, ip_prefix: str, gw: str) -> bool:
    try:
        return _run("linkhelm_netops.exe",
                    ["SET_ADDR", name, ip_prefix, gw]) .strip() == ""
    except OSError:
        return False


def set_dns(name: str, servers) -> bool:
    try:
        _run("linkhelm_netops.exe", ["SET_DNS", name, *servers])
        return True
    except OSError:
        return False


def add_route_verified(dest: str, mask: str, gw: str, if_idx: int):
    """Returns True when route added AND readback verified."""
    try:
        out = _run("linkhelm_netops.exe",
                   ["ADD_ROUTE", dest, mask, gw, str(if_idx)])
        return out.strip() == "VERIFIED"
    except OSError:
        return False


def del_route(dest: str, mask: str, gw: str) -> bool:
    try:
        _run("linkhelm_netops.exe", ["DEL_ROUTE", dest, mask, gw])
        return True
    except OSError:
        return False


def route_gateways(dest: str, mask: str):
    """Gateways currently serving dest/mask (deduped, empty list = none)."""
    try:
        out = _run("linkhelm_netops.exe", ["ROUTE_GATEWAYS", dest, mask])
        return [g for g in out.split() if g]
    except OSError:
        return []


def flush_dns() -> bool:
    try:
        _run("linkhelm_netops.exe", ["FLUSH_DNS"])
        return True
    except OSError:
        return False


def disable_interface(name: str) -> bool:
    try:
        _run("linkhelm_netops.exe", ["DISABLE_IF", name])
        return True
    except OSError:
        return False


# ---- ics ----

def ics_available() -> bool:
    return available("linkhelm_ics.exe")


def list_ethernet():
    """[{guid,name}] of connected physical ethernet adapters."""
    try:
        data = _run("linkhelm_ics.exe", ["LIST_ETHERNET"])
        return json.loads(data or "[]")
    except (OSError, ValueError):
        return None


def ics_enable(output_guid: str):
    """C++ ENABLE: returns the dict ({"error":..} on failure)."""
    try:
        data = _run("linkhelm_ics.exe", ["ENABLE", output_guid], timeout=60)
        return json.loads(data or "{}")
    except (OSError, ValueError) as exc:
        return {"error": f"C++ ICS module failed: {exc}"}


def ics_disable(state: dict):
    try:
        data = _run("linkhelm_ics.exe",
                    ["DISABLE", json.dumps(state, separators=(",", ":"))],
                    timeout=60)
        return json.loads(data or "{}")
    except (OSError, ValueError) as exc:
        return {"error": f"C++ ICS module failed: {exc}"}
