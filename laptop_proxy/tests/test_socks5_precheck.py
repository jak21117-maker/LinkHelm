"""Round-2 connectivity tests for the SOCKS5 precheck (check_socks5).

Covers the acceptance scenarios from the fix plan section 08:
greeting OK, method refused, bad reply, no listener, short reply.
These tests use a local threaded fake server — no admin rights, no routing changes.
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


class FakeSocksServer:
    """Minimal TCP server that replies according to `reply` bytes (None = close)."""

    def __init__(self, reply):
        self.reply = reply
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self._accepting = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while self._accepting:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    data = b""
                    while len(data) < 3:
                        chunk = conn.recv(3 - len(data))
                        if not chunk:
                            break
                        data += chunk
                    if self.reply is not None:
                        conn.sendall(self.reply)
                except OSError:
                    pass

    def stop(self):
        self._accepting = False
        self.server.close()


def test_precheck_ok_when_socks5_greeting_answered():
    srv = FakeSocksServer(b"\x05\x00")
    try:
        assert main.check_socks5("127.0.0.1", srv.port) == "SOCKS5_OK"
    finally:
        srv.stop()


def test_precheck_reports_method_refusal():
    srv = FakeSocksServer(b"\x05\xff")
    try:
        assert main.check_socks5("127.0.0.1", srv.port) == "SOCKS5_NO_ACCEPTABLE_METHOD"
    finally:
        srv.stop()


def test_precheck_reports_bad_reply():
    srv = FakeSocksServer(b"\x00\x01")
    try:
        assert main.check_socks5("127.0.0.1", srv.port) == "SOCKS5_BAD_REPLY"
    finally:
        srv.stop()


def test_precheck_reports_short_reply_when_server_closes():
    srv = FakeSocksServer(None)
    try:
        assert main.check_socks5("127.0.0.1", srv.port) == "SOCKS5_SHORT_REPLY"
    finally:
        srv.stop()


def test_precheck_reports_connect_failure_when_no_listener():
    # Pick a port that is almost certainly closed.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    result = main.check_socks5("127.0.0.1", closed_port, timeout=1.0)
    # Windows firewalls may silently drop (timeout) instead of sending RST;
    # both outcomes mean "TCP not reachable", which is what the precheck must report.
    assert result.startswith("TCP_CONNECT_FAILED(") or result == "TCP_CONNECT_TIMEOUT"


def test_precheck_rejects_invalid_target():
    assert main.check_socks5("", 8080) == "INVALID_TARGET"
    assert main.check_socks5(None, 8080) == "INVALID_TARGET"
    assert main.check_socks5("127.0.0.1", 0) == "INVALID_TARGET"
    assert main.check_socks5("127.0.0.1", 70000) == "INVALID_TARGET"
