"""v1.3.3 real-internet connectivity test.

Drives the EXACT production code path (main.HttpSocksBridge) with a real
SOCKS5 upstream (simulating the phone proxy) and then fetches REAL public
websites through it — proving the chain  客户端 → 本地桥接(18080) → SOCKS5 →
公网  can actually reach the internet, not just a loopback.

Marked offline-safe: if the machine itself has no internet, tests SKIP
(raise pytest.skip) instead of failing.
"""
import importlib
import socket
import sys
import time
import urllib.request

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from test_bridge_e2e import RealSocks5Server  # noqa: E402

main = importlib.import_module("main")


def _internet_ok():
    try:
        socket.create_connection(("223.5.5.5", 443), timeout=4).close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def bridge_chain():
    """SOCKS5 upstream (fake phone) + production HttpSocksBridge on 18081."""
    if not _internet_ok():
        pytest.skip("本机当前无互联网出口，跳过真实上网测试")
    socks = RealSocks5Server()
    bridge = main.HttpSocksBridge(18081, "127.0.0.1", socks.port)
    bridge.start()
    time.sleep(0.8)
    yield socks, bridge
    bridge.stop()
    socks.stop()


def _fetch(url):
    proxy_handler = urllib.request.ProxyHandler({
        "http": "http://127.0.0.1:18081",
        "https": "http://127.0.0.1:18081",
    })
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers={"User-Agent": "connectivity-test/1.0"})
    with opener.open(req, timeout=20) as resp:
        body = resp.read(4096)
        return resp.status, body


def test_http_website_via_bridge(bridge_chain):
    """Plain HTTP over the production bridge: real bytes from a public site."""
    status, body = _fetch("http://example.com/")
    assert status == 200
    assert b"Example Domain" in body


def test_https_website_via_bridge(bridge_chain):
    """HTTPS via CONNECT tunnel through the production bridge (TLS end-to-end)."""
    status, body = _fetch("https://www.baidu.com/")
    assert status == 200
    assert len(body) > 100


def test_tcp_reachability_of_bridge_listener(bridge_chain):
    """The bridge port must accept TCP like the phone's 8080 does in the field."""
    socks, _bridge = bridge_chain
    c = socket.create_connection(("127.0.0.1", 18081), timeout=5)
    c.sendall(b"PING\r\n")  # non-HTTP garbage must not crash the bridge
    c.close()
