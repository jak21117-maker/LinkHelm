"""Field-scenario regression tests (v1.3.1).

Reproduces the exact field report and proves the fix end to end, entirely in a
simulated environment (no phone, no admin rights):

Field report (Test-NetConnection 192.168.49.1 -Port 8080):
    PingSucceeded   : True     (17 ms)
    TcpTestSucceeded: False    ->  "TCP connect ... failed"

Simulated here on loopback:
    Phase 1  phone proxy NOT listening  -> ping OK + TCP refused + correct
             diagnosis ("network reachable, phone proxy not listening")
    Phase 2  phone proxy (v1.3.1 style) binds 0.0.0.0:8080
             -> TCP now CONNECTS, SOCKS5 handshake OK
    Phase 3  full data path: client -> local bridge(18080) -> SOCKS5 -> origin
    Phase 4  wildcard-bind reachability: a 0.0.0.0 listener answers on ANY
             local IPv4 (the essence of the Android-side fix)
"""
import importlib
import socket
import threading
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")

from test_bridge_e2e import OriginHTTPServer, RealSocks5Server  # noqa: E402

PREFERRED_PHONE_PORT = 8080
PREFERRED_BRIDGE_PORT = 18080


def _bind_port(preferred):
    """Try to bind the canonical port; fall back to an OS-assigned one."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", preferred))
    except OSError:
        s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tcp_reachable(host, port, timeout=2.0):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _wait_listening(port, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _tcp_reachable("127.0.0.1", port, timeout=0.3):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- Phase 1
def test_phase1_reproduces_field_report_no_listener():
    """No listener on the phone port: ping OK but TCP fails, with the right hint."""
    phone_port = _bind_port(PREFERRED_PHONE_PORT)  # reserved, nothing listening
    assert not _tcp_reachable("127.0.0.1", phone_port), "port must be closed here"

    precheck = main.check_socks5("127.0.0.1", phone_port)
    assert precheck.startswith("TCP_CONNECT_FAILED") or precheck == "TCP_CONNECT_TIMEOUT"

    # ICMP cross-check on loopback always answers == "PingSucceeded: True"
    assert main.ping_host("127.0.0.1") is True, "ICMP must be OK in this scenario"

    diagnosis = main.diagnose_tcp_failure("127.0.0.1", precheck)
    assert "not listening" in diagnosis
    assert "READY/SHARING" in diagnosis


# ---------------------------------------------------------------- Phase 2
def test_phase2_tcp_connects_after_phone_binds_wildcard():
    """The v1.3.1 fix: phone binds 0.0.0.0 -> TCP 8080 becomes reachable."""
    phone_port = _bind_port(PREFERRED_PHONE_PORT)
    socks = RealSocks5Server()
    # Re-bind the fake phone onto the canonical port with a WILDCARD bind,
    # mirroring the Android-side change (bindAddress 0.0.0.0).
    socks.stop()
    socks = RealSocks5Server()
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock.bind(("0.0.0.0", phone_port))
    srv_sock.listen(16)
    started = threading.Event()

    import select as _select
    import struct as _struct

    def serve():
        started.set()
        srv_sock.settimeout(0.5)
        while True:
            try:
                conn, _ = srv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _handle(conn):
        try:
            conn.settimeout(5)
            head = _recv_exact(conn, 2)
            if not head or head[0] != 0x05:
                conn.close()
                return
            _recv_exact(conn, head[1])  # methods
            conn.sendall(b"\x05\x00")
            head = _recv_exact(conn, 4)
            if not head or head[1] != 0x01:
                conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                conn.close()
                return
            atyp = head[3]
            if atyp == 0x01:
                addr = socket.inet_ntoa(_recv_exact(conn, 4))
            elif atyp == 0x03:
                ln = _recv_exact(conn, 1)[0]
                addr = _recv_exact(conn, ln).decode()
            else:
                conn.close()
                return
            port = _struct.unpack(">H", _recv_exact(conn, 2))[0]
            try:
                up = socket.create_connection((addr, port), timeout=5)
                conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            except OSError:
                conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                conn.close()
                return
            RealSocks5Server._relay(conn, up)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    assert started.wait(2)
    try:
        assert _wait_listening(phone_port), "simulated phone must be listening"
        # THE fix moment: TCP now connects and the SOCKS5 handshake completes.
        assert main.check_socks5("127.0.0.1", phone_port) == "SOCKS5_OK"
    finally:
        srv_sock.close()


# ---------------------------------------------------------------- Phase 3
def test_phase3_full_data_path_through_bridge_on_18080():
    """client -> bridge(18080) -> real SOCKS5 -> origin: bytes flow both ways."""
    socks = RealSocks5Server()
    origin = OriginHTTPServer()
    bridge_port = _bind_port(PREFERRED_BRIDGE_PORT)
    bridge = main.HttpSocksBridge(bridge_port, "127.0.0.1", socks.port, log_fn=lambda *a: None)
    bridge.start()
    try:
        assert _wait_listening(bridge_port)
        assert main.check_socks5("127.0.0.1", socks.port) == "SOCKS5_OK"

        req = (
            f"GET http://127.0.0.1:{origin.port}/v131 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin.port}\r\nConnection: close\r\n\r\n"
        ).encode()
        c = socket.create_connection(("127.0.0.1", bridge_port), timeout=10)
        c.sendall(req)
        c.settimeout(10)
        resp = b""
        while b"BRIDGE_E2E_OK" not in resp:
            chunk = c.recv(4096)
            if not chunk:
                break
            resp += chunk
        c.close()
        assert b"200" in resp.split(b"\r\n")[0]
        assert b"BRIDGE_E2E_OK" in resp
    finally:
        bridge.stop()
        socks.stop()
        origin.stop()


# ---------------------------------------------------------------- Phase 4
def test_phase4_wildcard_bind_reachable_on_any_local_ipv4():
    """0.0.0.0 listener answers on every local address (Android fix essence)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = _bind_port(PREFERRED_PHONE_PORT)
    srv.bind(("0.0.0.0", port))
    srv.listen(4)
    threading.Thread(target=srv.accept, daemon=True).start()

    local_ips = set()
    try:
        import psutil  # optional
        for _, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    local_ips.add(a.address)
    except ImportError:
        pass
    local_ips.add("127.0.0.1")
    try:
        hostname_ips = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        local_ips.update(ip[4][0] for ip in hostname_ips)
    except OSError:
        pass

    reachable = [ip for ip in local_ips if _tcp_reachable(ip, port, timeout=1.0)]
    srv.close()
    assert reachable, "wildcard bind must answer on at least one local IPv4"
    if len(local_ips) > 1:
        assert len(reachable) >= 2, (
            f"wildcard bind should answer on multiple local IPs, got {reachable} "
            f"(candidates: {sorted(local_ips)})"
        )
