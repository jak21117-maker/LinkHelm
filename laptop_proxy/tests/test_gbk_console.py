"""v1.3.2 Windows start-flow simulation tests.

Drives TunManager.start() end to end in a sandbox — real SOCKS5 upstream
(loopback "phone"), fake tun2socks process, and localized (GBK) Chinese
netsh/route output, exactly what zh-CN Windows emits.

Field bug being covered: text=True lost GBK netsh/route output (UTF-8 locale),
so the adapter was never detected ("Failed to create Virtual Adapter") and the
/32 return route was silently skipped — TCP to the phone worked but the VPN
never came up.
"""
import io
import importlib
import socket
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")

from test_bridge_e2e import RealSocks5Server  # noqa: E402

OUR_GW = main.OUR_TUN_GATEWAY


def gbk(text):
    return text.encode("gbk")


def netsh_interfaces_gbk(with_adapter=True):
    rows = [
        "管理员",
        "",
        "状态      类型        接口名称",
        "已连接    以太网      以太网",
        "已连接    WLAN        WLAN",
    ]
    if with_adapter:
        rows.append("已连接    未识别      LinkHelm")
    return gbk("\n".join(rows) + "\n")


def route_print_gbk(default_gw="192.168.49.1", extra_routes=()):
    lines = [
        "===========================================================================",
        "接口列表",
        " 12...aa bb cc dd ee ff ......WLAN",
        "===========================================================================",
        "",
        "IPv4 路由表",
        "===========================================================================",
        "活动路由:",
        "网络目标            网络掩码            网关                 接口   跃点数",
        f"          0.0.0.0          0.0.0.0     {default_gw:<15} 192.168.49.74     25",
        "        127.0.0.0        255.0.0.0        127.0.0.1        127.0.0.1    331",
        "      192.168.49.0    255.255.255.0        在链路上      192.168.49.74    281",
    ]
    lines.extend(extra_routes)
    return gbk("\n".join(lines) + "\n")


class FakeProcess:
    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class FakeConsole:
    """Stateful netsh/route fake with GBK Chinese output.

    Maintains a real route table: 'route add' inserts, 'route delete' removes,
    'route print' renders — so ownership checks and cleanup behave like Windows.
    """

    def __init__(self, default_gw="192.168.49.1", foreign_v1_gw=None, fail_add_masks=()):
        self.default_gw = default_gw
        self.fail_add_masks = tuple(fail_add_masks)
        # (dest, mask, gw) tuples currently in the table.
        self.routes = [("0.0.0.0", "0.0.0.0", default_gw)]
        if foreign_v1_gw:
            self.routes.append(("0.0.0.0", "128.0.0.0", foreign_v1_gw))
            self.routes.append(("128.0.0.0", "128.0.0.0", foreign_v1_gw))
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        exe = cmd[0].lower() if cmd else ""
        if exe == "netsh":
            if "show" in cmd and "interfaces" in cmd:
                return self._rc(0, netsh_interfaces_gbk(with_adapter=True))
            return self._rc(0, b"")
        if exe == "route":
            if "print" in cmd:
                return self._rc(0, self._render())
            if "add" in cmd:
                # route add <dest> mask <mask> <gw> [metric 1]
                dest, mask, gw = cmd[2], cmd[4], cmd[5]
                if any(m in cmd for m in self.fail_add_masks):
                    return self._rc(1, b"", gbk("请求的操作需要提升。"))
                if not any(r == (dest, mask, gw) for r in self.routes):
                    self.routes.append((dest, mask, gw))
                return self._rc(0, gbk("操作完成!"))
            if "delete" in cmd:
                # route delete <dest> mask <mask> <gw>
                dest, mask, gw = cmd[2], cmd[4], cmd[5]
                self.routes = [r for r in self.routes if r != (dest, mask, gw)]
                return self._rc(0, gbk("操作完成!"))
        return self._rc(0, b"")

    def _render(self):
        lines = [
            "===========================================================================",
            "接口列表",
            " 12...aa bb cc dd ee ff ......WLAN",
            "===========================================================================",
            "",
            "IPv4 路由表",
            "===========================================================================",
            "活动路由:",
            "网络目标            网络掩码            网关                 接口   跃点数",
        ]
        for dest, mask, gw in self.routes:
            lines.append(f"{dest:>16} {mask:>16} {gw:>17} 192.168.49.74      6")
        return gbk("\n".join(lines) + "\n")

    def has_route(self, dest, mask, gw):
        return (dest, mask, gw) in self.routes

    def calls_to(self, exe, verb):
        return [c for c in self.calls if c and c[0].lower() == exe and verb in c]

    @staticmethod
    def _rc(code, stdout=b"", stderr=b""):
        return type("R", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()


@pytest.fixture()
def socks_upstream():
    socks = RealSocks5Server()
    yield socks
    socks.stop()


def _make_tun(monkeypatch, console, socks, monkeypatch_deps=True):
    tun = main.TunManager("127.0.0.1", socks.port, 18080, log_fn=lambda *a: None)
    monkeypatch.setattr(main.subprocess, "run", console.run)
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: FakeProcess())
    if monkeypatch_deps:
        monkeypatch.setattr(tun, "_check_dependencies", lambda: "fake-tun2socks.exe")
    return tun


def test_start_flow_succeeds_with_gbk_console(monkeypatch, socks_upstream):
    console = FakeConsole()
    tun = _make_tun(monkeypatch, console, socks_upstream)

    tun.start()  # must NOT raise anywhere between precheck and routes

    assert tun.is_healthy()
    # The /32 return route to the phone must be pinned via the default gateway.
    assert tun._phone_route_gw == "192.168.49.1"
    assert console.has_route(tun.phone_ip, "255.255.255.255", "192.168.49.1")
    # Both /1 catch-all routes must be added via our TUN gateway.
    assert console.has_route("0.0.0.0", "128.0.0.0", OUR_GW)
    assert console.has_route("128.0.0.0", "128.0.0.0", OUR_GW)
    assert sorted(tun._owned_routes) == ["0.0.0.0", "128.0.0.0"]

    tun.stop()
    # Cleanup must remove exactly the routes we added — and only those.
    assert not console.has_route(tun.phone_ip, "255.255.255.255", "192.168.49.1")
    assert not console.has_route("0.0.0.0", "128.0.0.0", OUR_GW)
    assert not console.has_route("128.0.0.0", "128.0.0.0", OUR_GW)
    # The default route via the WLAN gateway must survive the whole lifecycle.
    assert console.has_route("0.0.0.0", "0.0.0.0", "192.168.49.1")


def test_adapter_detected_even_with_gbk_only_output(monkeypatch, socks_upstream):
    """The netsh adapter listing is pure GBK Chinese; detection must still work."""
    console = FakeConsole()
    tun = _make_tun(monkeypatch, console, socks_upstream)
    rc, out, _ = main._run_decoded(["netsh", "interface", "ipv4", "show", "interfaces"])
    # Reproduce the detection expression from start() against decoded output.
    assert "LinkHelm" in out
    tun.start()
    tun.stop()


def test_foreign_v1_routes_left_untouched(monkeypatch, socks_upstream):
    console = FakeConsole(foreign_v1_gw="198.18.0.1")
    tun = _make_tun(monkeypatch, console, socks_upstream)

    tun.start()
    # Foreign /1 routes exist: ours must NOT be added and theirs not deleted.
    assert tun._owned_routes == []
    assert not console.has_route("0.0.0.0", "128.0.0.0", OUR_GW)
    assert console.has_route("0.0.0.0", "128.0.0.0", "198.18.0.1")
    tun.stop()
    # Foreign routes must survive cleanup.
    assert console.has_route("0.0.0.0", "128.0.0.0", "198.18.0.1")
    assert console.has_route("128.0.0.0", "128.0.0.0", "198.18.0.1")


def test_route_add_failure_surfaces_decoded_gbk_detail(monkeypatch, socks_upstream):
    # /32 succeeds first, then the /1 adds fail -> rollback must remove the /32.
    console = FakeConsole(fail_add_masks=("128.0.0.0",))
    tun = _make_tun(monkeypatch, console, socks_upstream)

    with pytest.raises(RuntimeError) as excinfo:
        tun.start()
    assert "需要提升" in str(excinfo.value), "GBK error detail must be decoded, not mojibake"
    # Rollback must have removed the pinned /32 return route.
    assert not console.has_route(tun.phone_ip, "255.255.255.255", "192.168.49.1")
    assert tun._phone_route_gw is None


def test_run_decoded_decode_chain():
    assert main._run_decoded.__doc__  # sanity
    rc, out, err = main._run_decoded(["cmd.exe", "/c", "echo ok"])
    assert rc == 0 and "ok" in (out or "")
