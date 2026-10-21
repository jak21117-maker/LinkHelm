"""v1.4.3 — Windows UDP 检测（socks5_udp_dns_check）单元测试。

mock 一个最小的 SOCKS5 服务器（TCP 握手 + UDP ASSOCIATE + UDP 中继回 DNS 响应），
在真实 socket 上验证检测逻辑的正反两条路径；无管理员权限、无系统改动。
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


class MockSocksUdpRelay:
    """最小 SOCKS5 服务器：问候 + UDP ASSOCIATE + 中继回 DNS 响应。"""

    def __init__(self, deny_associate=False):
        self.deny_associate = deny_associate
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(4)
        self.port = self.tcp.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp_port = self.udp.getsockname()[1]
        self.udp.settimeout(0.5)
        self._open = True
        threading.Thread(target=self._serve_tcp, daemon=True).start()
        threading.Thread(target=self._serve_udp, daemon=True).start()

    def _serve_tcp(self):
        while self._open:
            try:
                conn, _ = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                conn.settimeout(3)
                greeting = b""
                while len(greeting) < 3:
                    part = conn.recv(3 - len(greeting))
                    if not part:
                        return
                    greeting += part
                conn.sendall(b"\x05\x00")
                request = b""
                while len(request) < 10:
                    part = conn.recv(10 - len(request))
                    if not part:
                        return
                    request += part
                if self.deny_associate:
                    conn.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
                conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00"
                             + self.udp_port.to_bytes(2, "big"))
                conn.settimeout(30)  # 保持控制连接直到测试结束
                while True:
                    if not conn.recv(256):
                        return
            except OSError:
                return

    def _serve_udp(self):
        while self._open:
            try:
                data, addr = self.udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) <= 10:
                continue
            payload = bytearray(data[10:])
            if len(payload) >= 12:
                payload[2] |= 0x80  # QR=1
            self.udp.sendto(bytes(data[:10]) + bytes(payload), addr)

    def close(self):
        self._open = False
        try:
            self.tcp.close()
        except OSError:
            pass
        try:
            self.udp.close()
        except OSError:
            pass


@pytest.fixture()
def relay():
    server = MockSocksUdpRelay()
    yield server
    server.close()


def test_udp_check_succeeds_through_relay(relay):
    ok, detail = main.socks5_udp_dns_check(
        "127.0.0.1", relay.port, dns_ip="127.0.0.1", dns_port=53, timeout=3.0
    )
    assert ok, detail
    assert "reachable" in detail


def test_udp_check_fails_when_associate_denied():
    server = MockSocksUdpRelay(deny_associate=True)
    try:
        ok, detail = main.socks5_udp_dns_check(
            "127.0.0.1", server.port, dns_ip="127.0.0.1", timeout=3.0
        )
    finally:
        server.close()
    assert not ok
    assert "rejected" in detail


def test_udp_check_fails_fast_when_no_listener():
    ok, detail = main.socks5_udp_dns_check(
        "127.0.0.1", 1, dns_ip="127.0.0.1", timeout=1.0
    )
    assert not ok
    assert "TCP" in detail


def test_dns_query_builder():
    query = main._build_dns_query(0x1234)
    assert query[:2] == b"\x12\x34"
    assert query[2] & 0x80 == 0          # 查询方向
    assert query.endswith(b"\x00\x01\x00\x01")
    assert b"\x07example\x03com\x00" in query
