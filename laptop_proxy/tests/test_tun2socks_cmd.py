"""v1.5.37 regression tests: tun2socks v2.7.0 (pflag) CLI compatibility.

v1.5.36 field failure: tun2socks v2.7.0 replaced Go's stdlib flag with pflag,
which requires the double-dash form for long flags. The bundled command still
used v2.5.2-era single-dash long flags (`-loglevel`), so pflag parsed it as
shorthand `-l` → "unknown shorthand flag: 'l'" → usage dump → process exits
before the adapter is ready → "连接启动失败".

The launch command MUST therefore only use pflag-safe spellings (long form
with `--`, or single-letter shorthands). `--`-form flags also work on
v2.5.2's stdlib flag, so one shape serves both bundled versions.
"""
import importlib
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


def _flag_tokens(cmd):
    return [tok for tok in cmd if tok.startswith("-")]


def test_tun2socks_cmd_uses_pflag_safe_long_flags():
    cmd = main.tun2socks_cmd("tun2socks.exe", "socks5://192.168.49.1:8080")
    flags = _flag_tokens(cmd)
    assert flags, "launch command must contain flags"
    for flag in flags:
        # v2.7.0 pflag: single-dash is ONLY valid for one-letter shorthands.
        # `-loglevel` (a word) would be parsed as `-l` + garbage → usage dump.
        assert not (flag.startswith("-") and not flag.startswith("--")), (
            f"single-dash long flag {flag!r} breaks tun2socks v2.7.0 (pflag); "
            "use the '--' long form"
        )


def test_tun2socks_cmd_keeps_required_flags_and_values():
    cmd = main.tun2socks_cmd("tun2socks.exe", "socks5://10.0.0.1:8080")
    joined = " ".join(cmd)
    # v1.5.41: the device URL carries the FIXED adapter GUID — same network
    # identity on every start/stop (no "LinkHelm 2/3/4", no fresh NLA profile).
    assert "--device" in cmd
    device = cmd[cmd.index("--device") + 1]
    assert device.startswith("tun://LinkHelm?guid=")
    guid = device.split("guid=", 1)[1]
    # Windows GUID form (CLSIDFromString-compatible, incl. braces).
    import re
    assert re.fullmatch(
        r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}", guid
    ), f"malformed adapter GUID: {guid!r}"
    assert guid == main.TUN_ADAPTER_GUID
    assert "--proxy" in cmd and "socks5://10.0.0.1:8080" in cmd
    assert "--loglevel" in cmd and "warning" in cmd
    assert "--tcp-auto-tuning" in joined
    assert cmd[0] == "tun2socks.exe"


def test_tun2socks_cmd_shape_matches_2_7_0_help():
    """The flags we pass must be a subset of the names tun2socks 2.7.0 exposes
    (verified by hand against `tun2socks -h` when the binary was upgraded)."""
    known_v270 = {"--device", "--proxy", "--loglevel", "--tcp-auto-tuning"}
    cmd = main.tun2socks_cmd("tun2socks.exe", "socks5://10.0.0.1:8080")
    assert set(_flag_tokens(cmd)) <= known_v270
