"""v1.5.36 regression tests: probe layering (liveness vs egress diagnostic).

v1.5.35 field bug: health/failover probes gated on a full SOCKS5 CONNECT
relay to 223.5.5.5:53. But the CONNECT egress is the PHONE's network — CN
cellular carriers filter outbound TCP/53 and some Android stacks restrict
CONNECT ports — so perfectly healthy tunnels were declared dead ("直接连不上").

v1.5.36 rule:
  - _probe_socks_port (health/failover/rebuild gates) = L1 TCP + L2 greeting.
    A greeting answer proves the SOCKS5 server is really alive (the original
    虚空握手 was bare TCP, which a dead proxy never answers).
  - _socks_relay_diag = L3 CONNECT, LOG-ONLY, never gates anything.

Each mock below is a real TCP server on loopback so the socket layering is
exercised end to end.
"""
import importlib
import socket
import threading
import time
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


class _MockSocks5Server:
    """Minimal SOCKS5 server: answers the greeting unless `answer_greeting`
    is False (a wedged / non-SOCKS listener). CONNECT behaviour is decided
    by `relay_reply`:
      "ok"      -> REP=0x00 success
      "refuse"  -> REP=0x01 general failure
      "silent"  -> never replies to CONNECT
      "none"    -> closes right after the greeting (no CONNECT phase)
    """

    def __init__(self, relay_reply, answer_greeting=True):
        self.relay_reply = relay_reply
        self.answer_greeting = answer_greeting
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _recv_exact(conn, n):
        """Read exactly n bytes — TCP may coalesce the greeting (3B) and the
        CONNECT (10B) into one segment; a naive recv() would leave stray
        bytes behind and desynchronise the state machine."""
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(10)
                if not self.answer_greeting:
                    time.sleep(0.1)  # wedged listener: no greeting, no close-RST yet
                    conn.close()
                    continue
                # v1.5.47: the client offers [no-auth, userpass] — VER +
                # NMETHOD (variable) instead of the old fixed 3-byte greeting.
                head = self._recv_exact(conn, 2)
                if len(head) < 2 or head[0:1] != b"\x05":
                    conn.close()
                    continue
                if self._recv_exact(conn, head[1]) is None:
                    conn.close()
                    continue
                conn.sendall(b"\x05\x00")
                if self.relay_reply == "none":
                    conn.close()
                    continue
                # CONNECT request: VER CMD RSV ATYP ADDR(4) PORT(2) = 10 bytes
                req = self._recv_exact(conn, 10)
                if len(req) < 10:
                    conn.close()
                    continue
                if self.relay_reply == "ok":
                    conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
                elif self.relay_reply == "refuse":
                    conn.sendall(b"\x05\x01\x00\x01" + b"\x00" * 6)
                elif self.relay_reply == "silent":
                    time.sleep(15)  # hold the socket without replying
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)
        try:
            self.sock.close()
        except OSError:
            pass


def _make_tun_manager(phone_port):
    return main.TunManager(
        phone_ip="127.0.0.1",
        phone_port=phone_port,
        local_port=0,
        log_fn=lambda *_a, **_k: None,
    )


def test_healthy_tunnel_survives_refused_egress():
    """THE v1.5.35 regression: proxy alive, egress refuses CONNECT (e.g.
    carrier filters TCP/53) — the tunnel MUST still count as healthy."""
    server = _MockSocks5Server("refuse")
    try:
        tm = _make_tun_manager(server.port)
        assert tm._probe_socks_port(server.port, timeout=2.0) is True, (
            "greeting-level liveness must not depend on CONNECT egress"
        )
        status = tm._socks_relay_diag(server.port, timeout=2.0)
        # beta1.0.3: the diag now appends the REP meaning after the hex code.
        assert status.startswith("refused(REP=0x01")
    finally:
        server.stop()


def test_healthy_tunnel_survives_silent_egress():
    """Egress never answers the CONNECT (filtered, no RST) — still healthy."""
    server = _MockSocks5Server("silent")
    try:
        tm = _make_tun_manager(server.port)
        assert tm._probe_socks_port(server.port, timeout=2.0) is True
        status = tm._socks_relay_diag(server.port, timeout=1.0)
        assert status == "egress-silent"
    finally:
        server.stop()


def test_dead_proxy_is_unhealthy():
    """TCP answers but no SOCKS5 greeting (wedged/foreign listener) — dead."""
    server = _MockSocks5Server("none", answer_greeting=False)
    try:
        tm = _make_tun_manager(server.port)
        assert tm._probe_socks_port(server.port, timeout=2.0) is False
    finally:
        server.stop()


def test_no_listener_is_unhealthy():
    tm = _make_tun_manager(1)  # port 1: nothing listens in tests
    assert tm._probe_socks_port(1, timeout=0.5) is False


def test_relay_diag_ok_when_egress_works():
    server = _MockSocks5Server("ok")
    try:
        tm = _make_tun_manager(server.port)
        assert tm._socks_relay_diag(server.port, timeout=2.0) == "ok"
    finally:
        server.stop()


def test_failover_monitor_keeps_lane_on_refused_egress(monkeypatch):
    """Failover switching decisions must be driven by greeting liveness, not
    egress: with a refusing-egress primary, one monitor pass must NOT
    accumulate a strike, so no needless lane switch happens."""
    server = _MockSocks5Server("refuse")
    try:
        tm = _make_tun_manager(server.port)
        tm._monitoring_active = True
        tm._restarting = False
        tm._dual_lane = False
        tm._use_lane_b = False
        tm._lane_a_fail_strikes = 0
        switches = []
        monkeypatch.setattr(tm, "_restart_tun2socks", lambda: switches.append(1))

        real_probe = tm._probe_socks_port
        calls = {"n": 0}

        def probe_then_stop(port, timeout=2.0):
            result = real_probe(port, timeout=timeout)
            calls["n"] += 1
            tm._monitoring_active = False  # exit the loop after one pass
            return result

        monkeypatch.setattr(tm, "_probe_socks_port", probe_then_stop)
        monkeypatch.setattr(main.time, "sleep", lambda _s: None)

        tm._monitor_lane_failover()
        assert calls["n"] == 1
        assert tm._lane_a_fail_strikes == 0
        assert switches == []
    finally:
        server.stop()
