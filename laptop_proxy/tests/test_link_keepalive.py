# -*- coding: utf-8 -*-
"""v1.4.61 — adaptive link-warmer (链路保温).

Semantics under test:
- healthy → 30s cadence, fully silent;
- degraded → 15s cadence (adaptive), transient blips never rebuild;
- v1.4.92: the warmer NEVER rebuilds the tunnel. First miss logs one 抖动
  line; every LINK_KEEPALIVE_FAST_STRIKES (3) consecutive misses logs one
  "tunnel kept" line and probing continues — the health monitor (with its
  long-probe gate) is the only rebuild decision-maker;
- the domestic site touch (百度/搜狗/360) only runs on healthy probes at the
  configured period, and its failure never triggers a rebuild.
"""
import http.client
import main


def _make_mgr(logs):
    mgr = object.__new__(main.TunManager)
    mgr.log = logs.append
    mgr.phone_ip = "192.168.49.1"
    mgr.phone_port = 8080
    mgr._monitoring_active = True
    return mgr


def _run_iterations(mgr, stop_after):
    """Drive the loop with time.sleep patched out; stops after N iterations."""
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= stop_after:
            mgr._monitoring_active = False

    main.time.sleep = fake_sleep
    main.TunManager._link_keepalive_loop(mgr)


class _FakeSock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendall(self, data):
        assert data == b"\x05\x01\x00"

    def recv(self, _n):
        return b"\x05\x00"


def test_healthy_link_is_fully_silent_and_domestic_touch_fires(monkeypatch):
    logs = []
    monkeypatch.setattr(
        main.socket, "create_connection", lambda *a, **k: _FakeSock(), raising=True
    )
    touched = {"n": 0}
    monkeypatch.setattr(
        main.TunManager, "_domestic_site_touch",
        lambda self, idx: touched.__setitem__("n", touched["n"] + 1),
    )
    mgr = _make_mgr(logs)
    _run_iterations(mgr, stop_after=25)
    assert logs == []
    # probes 10 and 20 are healthy round-probe multiples → 2 touches
    assert touched["n"] == 2, touched


def test_transient_blips_never_rebuild_and_recover_silently(monkeypatch):
    logs = []
    state = {"fail": 2}  # < 3 strikes, then recovers

    def flaky(*_a, **_k):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise OSError("blip")
        return _FakeSock()

    monkeypatch.setattr(main.socket, "create_connection", flaky, raising=True)
    mgr = _make_mgr(logs)
    fired = {"n": 0}
    monkeypatch.setattr(
        main.TunManager, "_restart_engine",
        lambda self: fired.__setitem__("n", fired["n"] + 1),
    )
    _run_iterations(mgr, stop_after=10)
    assert fired["n"] == 0
    # v1.4.92: first miss logs one 抖动 advisory; recovery stays silent.
    assert len(logs) == 1 and "jitter" in logs[0], logs


def test_sustained_loss_never_rebuilds_and_keeps_probing(monkeypatch):
    # v1.4.92: the warmer NEVER rebuilds — sustained loss only logs one
    # "tunnel kept" line per 3-strike window and probing continues; the health
    # monitor is the sole rebuild decision-maker.
    logs = []
    state = {"calls": 0}

    def dead(*_a, **_k):
        state["calls"] += 1
        raise OSError("link down")

    monkeypatch.setattr(main.socket, "create_connection", dead, raising=True)
    mgr = _make_mgr(logs)
    fired = {"n": 0}
    monkeypatch.setattr(
        main.TunManager, "_restart_engine",
        lambda self: fired.__setitem__("n", fired["n"] + 1),
    )
    # 8 probes ≈ 2 full 3-strike windows + 2 extra misses.
    _run_iterations(mgr, stop_after=8)
    assert fired["n"] == 0, "warmer must not rebuild the tunnel (v1.4.92)"
    assert len(logs) >= 2, "one jitter line + one line per 3-strike window"
    assert any("jitter" in line for line in logs)
    keep_lines = [line for line in logs if "tunnel kept" in line]
    assert len(keep_lines) == 2  # 3 and 6 consecutive misses
    assert "45s" in keep_lines[0]
    assert "90s" in keep_lines[1]
    assert state["calls"] == 7, "probing continues at degraded cadence"


def test_domestic_touch_failure_never_rebuilds(monkeypatch):
    logs = []

    class _DeadConn:
        def __init__(self, *a, **k):
            raise OSError("site down")

    monkeypatch.setattr(
        main.socket, "create_connection", lambda *a, **k: _FakeSock(), raising=True
    )
    monkeypatch.setattr(main.http.client, "HTTPConnection", _DeadConn)
    mgr = _make_mgr(logs)
    fired = {"n": 0}
    monkeypatch.setattr(
        main.TunManager, "_restart_engine",
        lambda self: fired.__setitem__("n", fired["n"] + 1),
    )
    _run_iterations(mgr, stop_after=24)  # crosses two domestic-touch windows
    assert fired["n"] == 0, "domestic site hiccups must not rebuild the tunnel"
    assert logs == []
