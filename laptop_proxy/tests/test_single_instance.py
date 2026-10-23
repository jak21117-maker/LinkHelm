# -*- coding: utf-8 -*-
"""beta1.0.5 — reconnect TAKES OVER + TRUE exit (VPN never outlives the app).

Semantics under test:
- _kill_engine_procs_all force-kills every hev-socks5-tunnel process
  (taskkill /T /F by image name + Stop-Process fallback) and reports the
  names it saw;
- TunManager.start() kills leftover processes and proceeds (v1.4.62 refused —
  users read that as "reconnect does nothing"); it only raises when the kill
  itself fails;
- the NetCfg connection hive (Control\\Network\\{4D36E972...}) is swept —
  that registry Connection\\Name is the actual source of the "LinkHelm
  32/41" climbing numbers, not the NLA profiles alone;
- TunManager.stop() reaps the whole process tree (taskkill /T /F by PID) so a
  silently-failed terminate can never leave the VPN running in background.
"""
import pytest

import engine
import main


# ---------------------------------------------------------------- helpers

def _cleanup_calls(monkeypatch, captured):
    def fake_run_decoded(cmd, timeout=0):
        captured.append((list(cmd), timeout))
        return 0, "", ""
    monkeypatch.setattr(main, "_run_decoded", fake_run_decoded)


# ------------------------------------------------- profile/device cleanup

def test_cleanup_sweeps_devices_netcfg_connections_and_profiles(monkeypatch):
    captured = []
    _cleanup_calls(monkeypatch, captured)
    logs = []
    main._cleanup_stale_tun_adapters(logs.append)

    assert captured, "cleanup must invoke PowerShell once"
    cmd, timeout = captured[0]
    assert cmd[:3] == ["powershell", "-NoProfile", "-Command"]
    ps = cmd[3]
    # devices: pnputil removal for legacy LinkHelm* Wintun adapters
    assert "Get-PnpDevice -FriendlyName 'LinkHelm*'" in ps
    assert "pnputil /remove-device" in ps
    # v1.4.63: NetCfg connection hive — the actual numbering source
    assert "Control\\Network\\{4D36E972-E325-11CE-BFC1-08002BE10318}" in ps
    assert "Connection" in ps and "LinkHelm*" in ps
    # v1.4.62: NLA profiles still swept too
    assert "Network\\Profiles" in ps
    assert "Remove-Item" in ps
    assert timeout == 60
    assert any("Cleaned up legacy leftover virtual NICs" in m for m in logs)
    # beta1.0.5: the CURRENT adapter must survive the sweep — the distinct
    # name (LinkHelm-IwIP, fixed GUID) is what makes "先清旧 → 建新 → 复用"
    # possible: the sweep only ever matches the OLD LinkHelm*/LaptopProxyVPN*
    # patterns and explicitly excludes the live adapter in all three layers.
    assert engine.ADAPTER_NAME == "LinkHelm-IwIP"
    assert ps.count("-ne 'LinkHelm-IwIP'") == 3   # PnP + NetCfg + NLA layers
    assert "$_.FriendlyName -ne 'LinkHelm-IwIP'" in ps


def test_cleanup_survives_tool_failure(monkeypatch):
    def boom(cmd, timeout=0):
        raise OSError("powershell missing")
    monkeypatch.setattr(main, "_run_decoded", boom)
    main._cleanup_stale_tun_adapters(lambda _m: None)  # must not raise


# ------------------------------------------------ foreign engine probe

def test_foreign_engine_reports_running_processes(monkeypatch):
    def fake_run_decoded(cmd, timeout=0):
        return 0, "hev-socks5-tunnel\n", ""
    monkeypatch.setattr(main, "_run_decoded", fake_run_decoded)
    assert main._find_foreign_engine_procs() == ("hev-socks5-tunnel",)


def test_foreign_engine_empty_when_none(monkeypatch):
    def fake_run_decoded(cmd, timeout=0):
        return 0, "", ""
    monkeypatch.setattr(main, "_run_decoded", fake_run_decoded)
    assert main._find_foreign_engine_procs() == ()


def test_foreign_engine_degrades_to_empty_on_error(monkeypatch):
    def boom(cmd, timeout=0):
        raise OSError("no powershell")
    monkeypatch.setattr(main, "_run_decoded", boom)
    assert main._find_foreign_engine_procs() == ()


# --------------------------------------------------- take-over kill path

def test_kill_engine_procs_all_uses_taskkill_tree_and_reports(monkeypatch):
    # first probe (pre-kill) sees a process, the post-kill verify sees none
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return ("hev-socks5-tunnel",) if calls["n"] == 1 else ()

    monkeypatch.setattr(main, "_find_foreign_engine_procs", probe)
    runs = []
    monkeypatch.setattr(
        main.subprocess, "run",
        lambda cmd, **k: runs.append(list(cmd)),
    )
    logs = []
    killed = main._kill_engine_procs_all(log_fn=logs.append)
    assert killed == ("hev-socks5-tunnel",)
    # taskkill /T /F by image name (tree kill) must be attempted
    assert any(
        cmd[0] == "taskkill" and "/T" in cmd and "/IM" in cmd for cmd in runs
    )
    assert any("All leftover engine processes have been ended" in m for m in logs)


def test_kill_engine_procs_all_noop_when_none(monkeypatch):
    monkeypatch.setattr(main, "_find_foreign_engine_procs", lambda: ())
    runs = []
    monkeypatch.setattr(
        main.subprocess, "run", lambda cmd, **k: runs.append(list(cmd)),
    )
    assert main._kill_engine_procs_all() == ()
    assert runs == []


# ------------------------------------------------------ start() take-over

def _make_mgr():
    mgr = object.__new__(main.TunManager)
    mgr.log = lambda _m: None
    mgr.phone_ip = "192.168.49.1"
    mgr.phone_port = 8080
    return mgr


def test_start_kills_stray_and_proceeds(monkeypatch):
    mgr = _make_mgr()
    monkeypatch.setattr(main, "_find_foreign_engine_procs", lambda: ())
    killed = []
    monkeypatch.setattr(
        main, "_kill_engine_procs_all", lambda log_fn=None: killed.append(1) or (),
    )
    monkeypatch.setattr(
        main, "check_socks5_with_retry", lambda *a, **k: "SOCKS5_OK",
    )
    cleanup_done = []
    monkeypatch.setattr(
        main, "_cleanup_stale_tun_adapters", lambda log_fn: cleanup_done.append(1),
    )

    def no_deps(self):
        raise RuntimeError("STOP-HERE")

    monkeypatch.setattr(main.TunManager, "_check_dependencies", no_deps)
    with pytest.raises(RuntimeError, match="STOP-HERE"):
        mgr.start()
    assert killed == [1]        # take-over kill always attempted
    # beta1.0.56: the tun-adapter cleanup is no longer auto-invoked from
    # start() — its `pnputil /remove-device` physically removes a kernel PnP
    # device and bluescreens when an orphaned adapter is still referenced. It is
    # now a manual-only hygiene tool, so the normal flow must NOT call it.
    assert cleanup_done == []  # tun-adapter cleanup is manual-only now


def test_start_raises_only_when_kill_fails(monkeypatch):
    mgr = _make_mgr()
    monkeypatch.setattr(
        main, "_find_foreign_engine_procs", lambda: ("hev-socks5-tunnel",),
    )
    monkeypatch.setattr(main, "_kill_engine_procs_all", lambda log_fn=None: ())
    monkeypatch.setattr(
        main, "check_socks5_with_retry",
        lambda *a, **k: pytest.fail("precheck must not run after failed kill"),
    )
    with pytest.raises(RuntimeError, match="could not be ended"):
        mgr.start()


# --------------------------------------------------- stop() tree reaping

def test_stop_reaps_process_tree(monkeypatch):
    mgr = object.__new__(main.TunManager)
    mgr.log = lambda _m: None
    proc = main.subprocess.Popen(
        ["cmd", "/c", "exit 0"],
        stdout=main.subprocess.PIPE, stderr=main.subprocess.PIPE,
    )
    proc.wait()
    mgr.process = proc
    mgr._monitoring_active = False
    monkeypatch.setattr(main.TunManager, "_notify_tunnel_lost", lambda self: None)
    monkeypatch.setattr(main.TunManager, "_clean_routes", lambda self: None)
    runs = []
    real_run = main.subprocess.run

    def spy(cmd, **k):
        runs.append(list(cmd))
        return real_run(cmd, **k)

    monkeypatch.setattr(main.subprocess, "run", spy)
    mgr.stop()
    assert proc.poll() is not None             # process is gone
    assert mgr.process is None
    assert any(
        cmd[0] == "taskkill" and "/T" in cmd and "/PID" in cmd for cmd in runs
    ), "stop must taskkill /T /F the tree as a last-resort guarantee"
    assert any(
        cmd[0] == "netsh" and "LinkHelm" in " ".join(cmd) for cmd in runs
    ), "stop must disable the LinkHelm adapter"
