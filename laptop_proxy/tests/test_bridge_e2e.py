"""End-to-end connectivity tests (v1.3).

Simulates the real data path without admin rights or a physical phone:

    HTTP client -> HttpSocksBridge(127.0.0.1:B) -> real SOCKS5 server
                  ("phone proxy", 127.0.0.1:S)  -> HTTP origin server

Verifies that data actually flows in both directions through the bridge and
the SOCKS5 upstream (full CONNECT handshake, not just a greeting check).
"""
import importlib
import select
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


class RealSocks5Server:
    """Minimal but real SOCKS5 server: greeting + CONNECT + full bidirectional relay."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.connections_served = 0
        self._accepting = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self._accepting:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            # --- greeting: VER(1) + NMETHODS(1) + METHODS(NMETHODS) ---
            data = self._recv_exact(conn, 2)
            if not data or data[0] != 0x05:
                conn.close()
                return
            nmethods = data[1]
            if not self._recv_exact(conn, nmethods):
                conn.close()
                return
            conn.sendall(b"\x05\x00")  # no auth
            # --- request ---
            head = self._recv_exact(conn, 4)
            if not head or head[1] != 0x01:  # only CONNECT
                conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                conn.close()
                return
            atyp = head[3]
            if atyp == 0x01:
                addr = socket.inet_ntoa(self._recv_exact(conn, 4))
            elif atyp == 0x03:
                ln = self._recv_exact(conn, 1)[0]
                addr = self._recv_exact(conn, ln).decode()
            elif atyp == 0x04:
                addr = socket.inet_ntop(socket.AF_INET6, self._recv_exact(conn, 16))
            else:
                conn.close()
                return
            port = struct.unpack(">H", self._recv_exact(conn, 2))[0]
            try:
                upstream = socket.create_connection((addr, port), timeout=5)
                conn.sendall(b"\x05\x00\x00\x01" + b"\x00\x00\x00\x00" + b"\x00\x00")
            except OSError:
                conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                conn.close()
                return
            self.connections_served += 1
            self._relay(conn, upstream)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    @staticmethod
    def _relay(a, b):
        sockets = [a, b]
        try:
            while True:
                r, _, e = select.select(sockets, [], sockets, 30)
                if e:
                    return
                for s in r:
                    other = b if s is a else a
                    chunk = s.recv(65536)
                    if not chunk:
                        return
                    other.sendall(chunk)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self):
        self._accepting = False
        self.server.close()


class OriginHTTPServer:
    """Tiny origin server the SOCKS5 proxy will CONNECT to."""

    MARKER = "BRIDGE_E2E_OK"

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = outer.MARKER.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


@pytest.fixture()
def e2e_env():
    socks = RealSocks5Server()
    origin = OriginHTTPServer()
    yield socks, origin
    socks.stop()
    origin.stop()


def test_precheck_ok_against_real_socks5_server(e2e_env):
    socks, _ = e2e_env
    # check_socks5 must pass a FULL greeting exchange with a real server.
    assert main.check_socks5("127.0.0.1", socks.port) == "SOCKS5_OK"


def test_http_get_through_bridge_reaches_origin(e2e_env):
    socks, origin = e2e_env
    port = _free_port()
    bridge = main.HttpSocksBridge(port, "127.0.0.1", socks.port, log_fn=lambda *a: None)
    bridge.start()
    try:
        assert _wait_listening(port), "bridge did not start listening"

        # Absolute-URI GET via the local bridge (browser/proxy style).
        req = (
            f"GET http://127.0.0.1:{origin.port}/e2e HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin.port}\r\nConnection: close\r\n\r\n"
        ).encode()
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(req)
        resp = b""
        c.settimeout(10)
        while b"BRIDGE_E2E_OK" not in resp:
            chunk = c.recv(4096)
            if not chunk:
                break
            resp += chunk
        c.close()
        assert b"200" in resp.split(b"\r\n")[0], resp[:200]
        assert b"BRIDGE_E2E_OK" in resp
        assert socks.connections_served >= 1, "SOCKS5 upstream was never used"
    finally:
        bridge.stop()


def test_http_connect_tunnel_through_bridge(e2e_env):
    socks, origin = e2e_env
    port = _free_port()
    bridge = main.HttpSocksBridge(port, "127.0.0.1", socks.port, log_fn=lambda *a: None)
    bridge.start()
    try:
        assert _wait_listening(port), "bridge did not start listening"

        # CONNECT tunnel via the bridge (tun2socks-style path).
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(
            f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin.port}\r\n\r\n".encode()
        )
        c.settimeout(10)
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(4096)
        assert head.startswith(b"HTTP/1.1 200"), head[:200]

        # Traffic now flows inside the tunnel to the origin through SOCKS5.
        c.sendall(f"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\nConnection: close\r\n\r\n".encode())
        resp = b""
        while b"BRIDGE_E2E_OK" not in resp:
            chunk = c.recv(4096)
            if not chunk:
                break
            resp += chunk
        c.close()
        assert b"200" in resp.split(b"\r\n")[0], resp[:200]
        assert b"BRIDGE_E2E_OK" in resp
        assert socks.connections_served >= 1, "SOCKS5 upstream was never used"
    finally:
        bridge.stop()
