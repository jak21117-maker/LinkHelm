# -*- coding: utf-8 -*-
"""beta1.0.5 — lwIP engine launch configuration (replaces the old
tun2socks-command-shape tests).

LwipEngine.build_launch must emit a YAML that matches hev-socks5-tunnel's
conf schema (src/hev-config.c, tag 2.19.1):
  - tunnel.name = LinkHelm-IwIP + tunnel.guid (fixed GUID → the SAME Wintun
    adapter is reopened every run, so Windows never piles up
    "LinkHelm-IwIP 2/3/4");
  - socks5.address / port / udp (+ username / password);
  - misc.* — connect-timeout & the read-write timeouts ONLY parse under
    `misc:`: hev's parser silently IGNORES unknown keys (a value left under
    `socks5:` would leave the 10s default in place — too short for the phone's
    Wi-Fi Direct dial). This is the regression these tests lock down.
"""
import os
import re

import pytest

import engine


# ---------------------------------------------------------------- helpers

def _parse_flat_yaml(text):
    """Minimal parser for the flat two-level YAML the engine emits.

    Returns {section: {key: raw_scalar_string}}. Values keep their quotes;
    callers strip them. Good enough for this schema (no nested maps, no
    lists) and keeps the test dependency-free.
    """
    doc = {}
    section = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            key, _, rest = line.partition(":")
            section = key.strip()
            doc.setdefault(section, {})
            if rest.strip():
                doc[section][""] = rest.strip()
            continue
        key, _, value = line.strip().partition(":")
        doc[section][key.strip()] = value.strip()
    return doc


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].replace("''", "'")
    return value


@pytest.fixture()
def launch(tmp_path, monkeypatch):
    """Run build_launch against an isolated per-user config dir."""
    monkeypatch.setattr(engine, "ENGINE_CONFIG_DIR", str(tmp_path / "engine"))
    (exe, args, cwd) = engine.LwipEngine().build_launch(
        "socks5://linkhelm:secret123@192.168.49.1:8080"
    )
    cfg_path = args[0]
    assert exe == engine.LWIP_EXE
    assert cwd == str(tmp_path / "engine")
    with open(cfg_path, encoding="utf-8") as fh:
        text = fh.read()
    return _parse_flat_yaml(text), text, cfg_path


# ------------------------------------------------------- adapter identity

def test_adapter_name_is_linkhelm_iwip(launch):
    doc, _text, _cfg = launch
    assert _unquote(doc["tunnel"]["name"]) == "LinkHelm-IwIP"


def test_adapter_has_fixed_guid_for_reuse(launch):
    doc, _text, _cfg = launch
    guid = _unquote(doc["tunnel"]["guid"])
    # hev_wintun_parse_guid demands exactly the 36-char 8-4-4-4-12 form.
    assert re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", guid
    )
    assert guid == engine.ADAPTER_GUID


def test_adapter_ipv4_matches_routing_design(launch):
    doc, _text, _cfg = launch
    # main.py routes 0.0.0.0/1 + 128.0.0.0/1 via OUR_TUN_GATEWAY (10.0.0.1)
    # and netsh-layers 10.0.0.2/24 on the adapter; the engine must assign
    # 10.0.0.1 itself so the gateway is valid from the first second.
    assert _unquote(doc["tunnel"]["ipv4"]) == "10.0.0.1"


# -------------------------------------------- misc-section timeouts (P0)

def test_timeouts_live_under_misc(launch):
    doc, _text, _cfg = launch
    misc = doc["misc"]
    assert misc["connect-timeout"] == "25000"
    assert misc["tcp-read-write-timeout"] == "300000"
    assert misc["udp-read-write-timeout"] == "60000"
    assert misc["max-session-count"] == "0"


def test_timeouts_are_absent_from_socks5_section(launch):
    """Regression: keys hev does not recognise under `socks5:` are SILENTLY
    ignored, so a timeout left there is the same as not configuring it."""
    doc, _text, _cfg = launch
    socks5 = doc["socks5"]
    for key in ("connect-timeout", "tcp-read-write-timeout",
                "udp-read-write-timeout", "max-session-count"):
        assert key not in socks5, f"{key} must be under misc:, not socks5:"


def test_socks5_section_is_valid(launch):
    doc, _text, _cfg = launch
    socks5 = doc["socks5"]
    assert _unquote(socks5["address"]) == "192.168.49.1"
    assert socks5["port"] == "8080"
    assert _unquote(socks5["udp"]) == "udp"
    assert _unquote(socks5["username"]) == "linkhelm"
    assert _unquote(socks5["password"]) == "secret123"


def test_no_pid_file_keeps_engine_in_foreground(launch):
    """Regression (P0): hev-main.c does `if (pid_file) run_as_daemon(...)` —
    configuring a pid-file makes the engine FORK into a detached daemon whose
    parent exits 0 immediately. main.py owns the engine lifecycle via Popen
    (crash-watchdog, terminate on stop), so the engine MUST stay foreground."""
    doc, _text, _cfg = launch
    assert "pid-file" not in doc["misc"]


# ------------------------------------------------- credentials & process

def test_credentials_never_reach_the_command_line(launch):
    """Creds go into the per-user YAML only — never argv/logs."""
    _doc, text, cfg_path = launch
    assert "linkhelm" in text and "secret123" in text  # inside the YAML file
    exe, args, _cwd = engine.LwipEngine().build_launch(
        "socks5://linkhelm:secret123@192.168.49.1:8080"
    )
    assert args == [cfg_path]  # config path is the ONLY argument
    assert "secret123" not in " ".join(args)


def test_config_is_written_per_user(launch):
    _doc, _text, cfg_path = launch
    assert cfg_path.startswith(engine.ENGINE_CONFIG_DIR)
    assert cfg_path.endswith("main.yml")


def test_no_credentials_when_anonymous(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "ENGINE_CONFIG_DIR", str(tmp_path / "engine"))
    _exe, args, _cwd = engine.LwipEngine().build_launch(
        "socks5://192.168.49.1:8080"
    )
    doc = _parse_flat_yaml(open(args[0], encoding="utf-8").read())
    assert "username" not in doc["socks5"]
    assert "password" not in doc["socks5"]


# -------------------------------------------- real-binary parse smoke test

def test_generated_config_is_accepted_by_real_engine(launch, tmp_path):
    """Integration: hand the generated YAML to the shipped
    hev-socks5-tunnel.exe. Without admin rights it cannot create the Wintun
    adapter, but it MUST get past config parsing — a parse failure exits with
    a different signature (nothing logged, non-zero exit)."""
    exe = engine.LWIP_EXE
    if not os.path.isfile(exe):
        pytest.skip("hev-socks5-tunnel.exe not shipped in this checkout")
    import subprocess
    log_path = tmp_path / "parse.log"
    doc, _text, cfg_path = launch
    # point the log at a temp file we can inspect
    text = open(cfg_path, encoding="utf-8").read().replace(
        doc["misc"]["log-file"], "'%s'" % str(log_path).replace("\\", "/")
    )
    cfg2 = tmp_path / "smoke.yml"
    cfg2.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [exe, str(cfg2)], capture_output=True, timeout=15,
        cwd=os.path.dirname(exe),
    )
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    # The engine reached the tunnel-open step (config parsed) — it then fails
    # on the Wintun adapter because the test process is not elevated.
    # "set limit nofile" (INFO) is logged only after the config file parsed,
    # and "socks5 tunnel open" (ERROR) is the very next milestone.
    assert "set limit nofile" in log, f"config not parsed; log={log!r}"
    assert "socks5 tunnel open" in log
    assert proc.returncode != 0  # no admin → cannot open the adapter
