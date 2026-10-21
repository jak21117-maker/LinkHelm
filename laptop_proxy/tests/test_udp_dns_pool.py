# -*- coding: utf-8 -*-
"""v1.5.1 — UDP/DNS candidate pool (DNS 候选池，仅诊断).

Semantics under test:
- probes run in order: sticky primary (if any) first, then the remaining
  UDP_CHECK_DNS_POOL entries;
- the first resolver that answers is remembered for the rest of the session
  as the sticky probe order (DIAGNOSTIC ONLY — the TUN adapter's DNS is NOT
  touched; the v1.4.93 automatic netsh reorder was removed in v1.5.1 as a
  game-flapping risk);
- when every candidate fails, no primary is remembered.
"""
import main


class _Var:
    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v


class _Root:
    def after(self, _delay, fn):
        fn()


class _Button:
    def configure(self, **_k):
        pass


def _make_app():
    app = object.__new__(main.App)
    app.phone_ip = _Var("192.168.49.1")
    app.phone_port = _Var("8080")
    app.log = lambda _msg: None
    app.root = _Root()
    app.udp_button = _Button()
    return app


def test_udp_check_iterates_pool_and_sticks_on_first_success(monkeypatch):
    app = _make_app()
    calls = []

    def flaky(host, port, dns_ip="223.5.5.5", **_k):
        calls.append(dns_ip)
        if dns_ip == "119.29.29.29":
            return False, "UDP 握手超时"
        return True, "ok"

    monkeypatch.setattr(main, "socks5_udp_dns_check", flaky, raising=True)

    app._do_udp_check()

    # 119 first (default primary), 223.5.5.5 rescues; sticky probe order.
    assert calls == ["119.29.29.29", "223.5.5.5"]
    assert app._udp_dns_primary == "223.5.5.5"

    # Second check starts at the sticky primary and needs no fallback.
    calls.clear()
    monkeypatch.setattr(
        main, "socks5_udp_dns_check",
        lambda host, port, dns_ip="223.5.5.5", **_k: (calls.append(dns_ip), (True, "ok"))[1],
        raising=True,
    )
    app._do_udp_check()
    assert calls == ["223.5.5.5"]


def test_udp_check_is_diagnostic_only_and_reports_failure(monkeypatch):
    """v1.5.1: a successful probe must NOT touch the adapter's DNS (netsh)."""
    app = _make_app()
    calls = []

    def ok(host, port, dns_ip="223.5.5.5", **_k):
        calls.append(dns_ip)
        return True, "ok"

    monkeypatch.setattr(main, "socks5_udp_dns_check", ok, raising=True)
    ran_netsh = []
    monkeypatch.setattr(
        main.subprocess, "run",
        lambda *a, **k: ran_netsh.append(a), raising=True,
    )

    app._do_udp_check()

    assert calls == ["119.29.29.29"], "first candidate succeeds immediately"
    assert ran_netsh == [], "diagnostic probe must never reconfigure the adapter DNS"


def test_udp_check_reports_failure_across_whole_pool(monkeypatch):
    app = _make_app()
    calls = []

    def dead(host, port, dns_ip="223.5.5.5", **_k):
        calls.append(dns_ip)
        return False, f"{dns_ip} 握手超时"

    monkeypatch.setattr(main, "socks5_udp_dns_check", dead, raising=True)

    app._do_udp_check()

    assert calls == list(main.UDP_CHECK_DNS_POOL), "every candidate must be tried"
    assert getattr(app, "_udp_dns_primary", None) is None, "no sticky primary on total failure"
