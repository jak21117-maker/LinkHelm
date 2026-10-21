import importlib
from pathlib import Path
import sys
import threading

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


def test_health_monitor_three_strikes_then_restart(monkeypatch):
    """v1.4.2: strike 1 waits, strike 2 stops sharing, strike 3 rebuilds tunnel."""
    events = []
    tunnel = main.TunManager("192.168.49.1", 8080, 8081, lambda m: events.append(m),
                             on_tunnel_lost=lambda: events.append("lost"))
    tunnel._monitoring_active = True
    tunnel._connection_healthy = True

    def disconnected(*_args, **_kwargs):
        return False

    monkeypatch.setattr(tunnel, "_probe_ok", disconnected)

    restart_calls = []

    def fake_restart():
        restart_calls.append(True)
        tunnel._monitoring_active = False

    monkeypatch.setattr(tunnel, "_restart_tun2socks", fake_restart)
    monkeypatch.setattr(main.time, "sleep", lambda _d: None)
    tunnel._monitor_health()

    joined = "\n".join(events)
    assert "1/3" in joined and "2/3" in joined
    assert events.count("lost") == 1          # sharing stopped exactly once (strike 2)
    assert restart_calls == [True]            # tunnel rebuilt only at strike 3
    assert not tunnel._connection_healthy


def test_health_monitor_single_blip_self_heals(monkeypatch):
    """v1.4.2: one failed cycle (probe + confirm) must NOT stop sharing or rebuild."""
    events = []
    tunnel = main.TunManager("192.168.49.1", 8080, 8081, lambda m: events.append(m),
                             on_tunnel_lost=lambda: events.append("lost"))
    tunnel._monitoring_active = True
    tunnel._connection_healthy = True

    probes = {"n": 0}

    def flaky(_timeout=4.0):
        probes["n"] += 1
        return probes["n"] > 2  # first cycle: probe + confirm both fail, then recover

    monkeypatch.setattr(tunnel, "_probe_ok", flaky)

    def sleep(_duration):
        if probes["n"] >= 4:  # a couple of healthy cycles observed → end loop
            tunnel._monitoring_active = False

    monkeypatch.setattr(main.time, "sleep", sleep)
    tunnel._monitor_health()

    joined = "\n".join(events)
    assert "1/3" in joined                      # blip logged, waiting to self-heal
    assert "Proxy connection recovered" in joined            # then recovered
    assert "lost" not in events                 # sharing never stopped
    assert not any("2/3" in e for e in events)  # second strike never reached
    assert tunnel._connection_healthy


def test_tunnel_loss_callback_stops_active_console_sharing():
    class FakeSharing:
        needs_cleanup = True
        stopped = False

        def stop(self):
            self.stopped = True
            self.needs_cleanup = False

    class FakeRoot:
        def after(self, _delay, callback):
            callback()

    class FakeStatus:
        value = None

        def set(self, value):
            self.value = value

    app = main.App.__new__(main.App)
    app.ics_mgr = FakeSharing()
    app._bridge_lock = threading.RLock()
    app.root = FakeRoot()
    app.bridge_status = FakeStatus()
    app.log = lambda _message: None
    app._update_bridge_button = lambda: None
    app._on_tunnel_lost()
    assert app.ics_mgr.stopped
    assert "Link lost" in app.bridge_status.value
