# -*- coding: utf-8 -*-
"""beta1.0.51 — two-stage UDP check (经隧道判定 + 直连中继诊断).

Semantics under test:
- the VERDICT comes from the THROUGH-TUNNEL probe (`tunnel_udp_dns_check`):
  TUN -> lwIP engine -> phone relay — the exact path games and the Windows
  DNS client take;
- probes run in order: sticky primary (if any) first, then the remaining
  UDP_CHECK_DNS_POOL entries;
- the first resolver that answers via the tunnel is remembered for the rest
  of the session as the sticky probe order (DIAGNOSTIC ONLY — the TUN
  adapter's DNS is never touched);
- when every through-tunnel candidate fails, a SECOND stage runs direct
  relay probes (`socks5_udp_dns_check`) as a diagnosis only: if the relay
  answers, the reported break is INSIDE the tunnel, not the phone;
- no sticky primary is remembered on total failure.
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

    def flaky(dns_ip="223.5.5.5", **_k):
        calls.append(dns_ip)
        if dns_ip == "119.29.29.29":
            return False, "DNS 查询超时"
        return True, "ok"

    monkeypatch.setattr(main, "tunnel_udp_dns_check", flaky, raising=True)
    relay_calls = []
    monkeypatch.setattr(
        main, "socks5_udp_dns_check",
        lambda *a, **k: relay_calls.append(a) or (False, "must not run"),
        raising=True,
    )

    app._do_udp_check()

    # 119 first (default primary), 223.5.5.5 rescues the through-tunnel verdict;
    # the direct relay diagnostic must never run when the tunnel works.
    assert calls == ["119.29.29.29", "223.5.5.5"]
    assert relay_calls == []
    assert app._udp_dns_primary == "223.5.5.5"

    # Second check starts at the sticky primary and needs no fallback.
    calls.clear()
    monkeypatch.setattr(
        main, "tunnel_udp_dns_check",
        lambda dns_ip="223.5.5.5", **_k: (calls.append(dns_ip), (True, "ok"))[1],
        raising=True,
    )
    app._do_udp_check()
    assert calls == ["223.5.5.5"]


def test_udp_check_is_diagnostic_only_and_reports_success(monkeypatch):
    """A successful probe must NOT touch the adapter's DNS (netsh)."""
    app = _make_app()
    calls = []

    def ok(dns_ip="223.5.5.5", **_k):
        calls.append(dns_ip)
        return True, "ok"

    monkeypatch.setattr(main, "tunnel_udp_dns_check", ok, raising=True)
    ran_netsh = []
    monkeypatch.setattr(
        main.subprocess, "run",
        lambda *a, **k: ran_netsh.append(a), raising=True,
    )

    app._do_udp_check()

    assert calls == ["119.29.29.29"], "first candidate succeeds immediately"
    assert ran_netsh == [], "diagnostic probe must never reconfigure the adapter DNS"
    assert app._udp_dns_primary == "119.29.29.29"


def test_udp_check_reports_failure_across_whole_pool(monkeypatch):
    app = _make_app()
    tunnel_calls = []
    relay_calls = []

    def dead_tunnel(dns_ip="223.5.5.5", **_k):
        tunnel_calls.append(dns_ip)
        return False, f"{dns_ip} DNS 查询超时"

    def dead_relay(*_a, dns_ip="223.5.5.5", **_k):
        relay_calls.append(dns_ip)
        return False, f"{dns_ip} UDP 握手超时"

    monkeypatch.setattr(main, "tunnel_udp_dns_check", dead_tunnel, raising=True)
    monkeypatch.setattr(main, "socks5_udp_dns_check", dead_relay, raising=True)

    app._do_udp_check()

    assert tunnel_calls == list(main.UDP_CHECK_DNS_POOL), \
        "every through-tunnel candidate must be tried before the diagnostic"
    assert relay_calls == list(main.UDP_CHECK_DNS_POOL), \
        "the relay diagnostic also walks the whole pool"
    assert getattr(app, "_udp_dns_primary", None) is None, \
        "no sticky primary on total failure"


def test_udp_check_relay_ok_means_break_is_inside_tunnel(monkeypatch):
    """beta1.0.51 P0: tunnel probes all fail but the phone relay answers →
    the report must point INSIDE the tunnel (adapter/routes/engine), not at
    the phone — this is the "UDP OK yet games dead" false-positive fix."""
    app = _make_app()
    monkeypatch.setattr(
        main, "tunnel_udp_dns_check",
        lambda dns_ip="223.5.5.5", **_k: (False, "DNS 查询超时"),
        raising=True,
    )
    monkeypatch.setattr(
        main, "socks5_udp_dns_check",
        lambda *a, **k: (True, "ok"),
        raising=True,
    )

    app._do_udp_check()

    assert getattr(app, "_udp_dns_primary", None) is None, \
        "a relay-only answer must never become the sticky verdict"
