# -*- coding: utf-8 -*-
"""beta1.0.61 regression tests.

Covers the reported bugs:
  1. airport (clash) subscriptions — browser-UA download, panel error text,
     local provider inlining (mihomo never talks to the panel itself);
  2. "one tap and the net drops" — rule mode is now an ABSOLUTE graft: only
     the store domains may leave via the node, everything else is forced
     DIRECT, and the subscription's own rules (airport MATCH,<node> tails
     included) are dropped;
  3. the accelerator / file transfer must not touch WLAN/Wi-Fi/adapters —
     a static audit of the modules plus the runtime subprocess surface;
  4. "phone sends jpg, PC saves bin" — Unicode-safe names + magic-byte
     format detection on both ends;
  5. "occasionally disconnects" — the receiver binds the wildcard (survives
     Wi-Fi Direct lease changes) and stays armed for several files.
"""

import ast
import base64
import hashlib
import io
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_transfer as ft
import store_accel

USER, PASS = "u", "p"


@pytest.fixture()
def paired(tmp_path, monkeypatch):
    """Point the pairing store at a temp file and pair loopback, mirroring
    the real two-end handshake."""
    store = tmp_path / "ft_paired.json"
    monkeypatch.setattr(ft, "_pair_store_path", lambda: str(store))
    ft.add_paired_ip("127.0.0.1")
    yield
    ft.remove_paired_ip("127.0.0.1")


@pytest.fixture()
def no_proxy_env(monkeypatch):
    """urllib honours *_proxy env vars; the sandbox sets them, a normal
    Windows box does not. Drop them for the duration of a test."""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _b64(text):
    return base64.b64encode(text.encode()).decode()


class _Panel:
    """A tiny local 'airport' that behaves like the real ones:

    - 403 + a Laravel-style error page ("token is error") unless the request
      carries a desktop-browser User-Agent;
    - serves a subscription body (Clash YAML / base64 URI list / provider
      config) at /get?token=... .
    """

    def __init__(self, body, status=200, require_browser_ua=True):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.body = body if isinstance(body, bytes) else body.encode()
        self.status = status
        self.require_browser_ua = require_browser_ua
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                outer.requests.append(dict(path=self.path,
                                           ua=self.headers.get("User-Agent", "")))
                if outer.require_browser_ua and "Mozilla" not in \
                        self.headers.get("User-Agent", ""):
                    payload = (b"<!DOCTYPE html><html><head><title>Forbidden"
                               b"</title></head><body><h1>Forbidden</h1>"
                               b"<p>token is error</p></body></html>")
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if outer.status != 200:
                    # A rejected token answers 403 + the Laravel error page
                    # even for a browser UA — verified against the real
                    # get.cctvclient.cn panel with an expired token.
                    payload = (b"<!DOCTYPE html><html><head><title>Forbidden"
                               b"</title></head><body><h1>Forbidden</h1>"
                               b"<div>403</div><div>token is error</div>"
                               b"</body></html>")
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/cctv/user/client/get?token=TEST" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FULL_CLASH = """port: 7890
socks-port: 7891
mode: rule
log-level: info
proxies:
  - name: "Node-A"
    type: ss
    server: a.example.com
    port: 8388
    cipher: aes-256-gcm
    password: "pw"
  - name: "Node-B"
    type: trojan
    server: b.example.com
    port: 443
    password: "pw2"
proxy-groups:
  - name: "Manual Switch"
    type: select
    proxies:
      - Node-A
      - Node-B
  - name: "Auto"
    type: url-test
    proxies:
      - Node-A
    url: https://www.gstatic.com/generate_204
    interval: 300
rules:
  - GEOSITE,cn,DIRECT
  - GEOIP,cn,DIRECT
  - MATCH,Manual Switch
"""

PROVIDER_CLASH = """mixed-port: 7890
mode: rule
proxy-providers:
  sub:
    type: http
    url: "%s"
    interval: 86400
    path: ./sub.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
proxy-groups:
  - name: "Airport Pick"
    type: select
    use:
      - sub
rules:
  - MATCH,Airport Pick
"""


# ---------------------------------------------------------------------------
# 1. subscription download: browser UA + panel error text
# ---------------------------------------------------------------------------

def test_subscription_download_uses_a_browser_ua(no_proxy_env):
    panel = _Panel(_b64("ss://aaa@a.example.com:8388#Node"))
    try:
        body, err = store_accel._download_subscription(panel.url)
        assert err is None, err
        assert "ss://" in base64.b64decode(body).decode()
        assert panel.requests, "the panel saw no request"
        assert "Mozilla" in panel.requests[0]["ua"]
    finally:
        panel.close()


def test_subscription_403_surfaces_the_panel_message(no_proxy_env):
    """A rejected token must say WHY (token expired / already used), not a
    bare 403 — the user can then act on it."""
    panel = _Panel("irrelevant", status=403)
    try:
        body, err = store_accel._download_subscription(panel.url)
        assert body is None
        assert err is not None
        assert "403" in err
        assert "token is error" in err
    finally:
        panel.close()


def test_panel_error_text_extraction():
    html = ("<!DOCTYPE html><html><head><title>Forbidden</title></head>"
            "<body><h1>Forbidden</h1><p>token is error</p></body></html>")
    assert store_accel._panel_error_text(html) == "token is error"
    assert store_accel._panel_error_text("<html><title>Nope</title></html>") == "Nope"
    assert store_accel._panel_error_text("") == ""


# ---------------------------------------------------------------------------
# 2. clash subscription shapes
# ---------------------------------------------------------------------------

def test_full_clash_yaml_passes_through(no_proxy_env):
    panel = _Panel(FULL_CLASH)
    try:
        body, err = store_accel._download_subscription(panel.url)
        cfg = store_accel._normalize_subscription_body(body)
        assert store_accel._has_nodes(cfg)
        assert "Node-A" in cfg
    finally:
        panel.close()


def test_base64_uri_list_is_converted(no_proxy_env):
    uris = "\n".join([
        "ss://" + base64.b64encode(b"aes-256-gcm:pw").decode() +
        "@a.example.com:8388#SS%20Node",
        "vmess://" + base64.b64encode(json.dumps({
            "v": "2", "ps": "VMess Node", "add": "b.example.com",
            "port": "443", "id": "b831381d-6324-4d53-ad4f-8cda48b30886",
            "aid": "0", "net": "ws", "tls": "tls", "path": "/ray",
        }).encode()).decode(),
        "trojan://pw@c.example.com:443?sni=c.example.com#Trojan%20Node",
    ])
    panel = _Panel(_b64(uris))
    try:
        body, err = store_accel._download_subscription(panel.url)
        cfg = store_accel._normalize_subscription_body(body)
        assert "proxies:" in cfg
        for t in ("ss", "vmess", "trojan"):
            assert 'type: "%s"' % t in cfg
    finally:
        panel.close()


# ---------------------------------------------------------------------------
# 3. provider inlining — the kernel never talks to the panel
# ---------------------------------------------------------------------------

def test_providers_are_inlined_locally(no_proxy_env):
    """An airport clash config whose proxy-providers point back at the panel
    is resolved HERE (browser UA) and inlined — mihomo never downloads it
    itself (panels 403 the kernel's UA, leaving only COMPATIBLE)."""
    nodes = "\n".join([
        "ss://" + base64.b64encode(b"aes-256-gcm:pw").decode() +
        "@a.example.com:8388#SS%20Node",
        "trojan://pw@c.example.com:443#Trojan%20Node",
    ])
    provider_panel = _Panel(_b64(nodes))
    try:
        cfg_text = PROVIDER_CLASH % provider_panel.url
        inlined = store_accel._inline_providers(cfg_text)
        assert "proxy-providers:" not in inlined
        assert 'name: "SS Node"' in inlined
        assert 'name: "Trojan Node"' in inlined
        # the group now references real node NAMES, not the provider
        assert "use:" not in inlined
        assert store_accel._first_select_group(inlined) == "Airport Pick"
    finally:
        provider_panel.close()


def test_provider_inlining_survives_a_dead_provider():
    cfg_text = PROVIDER_CLASH % "http://127.0.0.1:1/dead"
    out = store_accel._inline_providers(cfg_text, log=lambda _m: None)
    # nothing could be inlined — the config is returned untouched so the
    # caller's own validation reports it
    assert "proxy-providers:" in out


# ---------------------------------------------------------------------------
# 4. the graft guarantee: rule mode can ONLY proxy the store domains
# ---------------------------------------------------------------------------

def test_rule_mode_drops_the_subscriptions_match_tail():
    """An airport MATCH,<node> tail would send the whole PC through the node
    — a dead node then looks like a total outage. Rule mode keeps ONLY the
    store rules."""
    cfg = store_accel._build_config_from_user_yaml(FULL_CLASH, "rule",
                                                   "Manual Switch")
    rules = cfg[cfg.index("\nrules:"):]
    assert "DOMAIN-SUFFIX,steampowered.com,Manual Switch" in rules
    assert "DOMAIN-SUFFIX,epicgames.com,Manual Switch" in rules
    # every non-store destination is forced DIRECT
    assert rules.rstrip().endswith("MATCH,DIRECT")
    assert "MATCH,Manual Switch" not in rules
    assert "GEOSITE" not in rules
    assert "GEOIP" not in rules


def test_generated_config_forces_direct_for_everything_else():
    """The provider-based generator (base64 URI path) is equally strict."""
    cfg = store_accel.generate_config_text("https://panel.example/sub?token=x")
    assert "MATCH,DIRECT" in cfg
    assert "DOMAIN-SUFFIX,steampowered.com,STORE" in cfg


# ---------------------------------------------------------------------------
# 5. no WLAN / Wi-Fi / adapter contact — static audit + runtime surface
# ---------------------------------------------------------------------------

_FORBIDDEN_TOKENS = (
    "netsh", "wlan", "Wlan", "WLAN", "pnputil", "devcon", "Get-NetAdapter",
    "NetAdapter", "wintun", "Wintun", "ipconfig", "route add",
    "Set-NetIPAddress", "Disable-NetAdapter", "Enable-NetAdapter",
    "HNetCfg", "InternetConnectionSharing",
)


def _code_without_strings(path):
    """Source with every string literal removed (docstrings included), so a
    static audit only ever sees executable tokens — never prose."""
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.mark.parametrize("module,what", [
    (store_accel, "the accelerator must be a pure graft "
                  "(system proxy + loopback listener only)"),
    (ft, "file transfer must not touch WLAN/Wi-Fi/adapters either"),
])
def test_no_wlan_or_adapter_contact(module, what):
    code = _code_without_strings(module.__file__)
    for token in _FORBIDDEN_TOKENS:
        assert token not in code, (
            "%s references %r — %s" % (os.path.basename(module.__file__),
                                       token, what))


def test_accel_spawns_only_the_mihomo_child(monkeypatch, tmp_path):
    """Runtime audit: the ONLY subprocess the accelerator ever starts is the
    bundled mihomo binary."""
    spawned = []

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        class stderr:
            @staticmethod
            def readline():
                return b""

    def fake_popen(args, *a, **kw):
        spawned.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(store_accel.subprocess, "Popen", fake_popen)
    acc = store_accel.StoreAccel(str(tmp_path), lambda _m: None)
    body = _b64("ss://" + base64.b64encode(b"aes-256-gcm:pw").decode() +
                "@a.example.com:8388#Node")
    monkeypatch.setattr(store_accel, "_download_subscription",
                        lambda url, timeout=20: (body, None))
    monkeypatch.setattr(store_accel.StoreAccel, "_wait_ready",
                        lambda self, timeout=15: (True, ""))
    # beta1.0.63: NO system-proxy writer exists any more — the accelerator is a
    # loopback-only micro-kernel that coexists with Clash Verge.
    assert not hasattr(store_accel, "write_system_proxy")
    assert not hasattr(store_accel.StoreAccel, "enable_system_proxy")
    ok, _msg = acc.start("https://panel.example/sub?token=x", "rule")
    assert ok
    assert len(spawned) == 1
    assert os.path.basename(spawned[0][0]).lower().startswith("mihomo")
    acc.stop()


def test_accel_module_exposes_no_system_proxy_writer():
    """beta1.0.63: the accelerator must NOT be able to write the system proxy —
    that write path is what once clobbered Clash Verge's proxy and broke the
    user's network. Its only system-proxy touch is the read-only inspector."""
    assert not hasattr(store_accel, "write_system_proxy")
    assert not hasattr(store_accel, "enable_system_proxy")
    assert not hasattr(store_accel.StoreAccel, "enable_system_proxy")
    assert not hasattr(store_accel.StoreAccel, "disable_system_proxy")
    assert hasattr(store_accel, "read_system_proxy")   # read-only display only


def test_watchdog_does_not_restore_a_proxy_it_never_set(monkeypatch, tmp_path):
    """beta1.0.63: the kernel has no proxy to restore — _watchdog must just log."""
    logged = []
    acc = store_accel.StoreAccel(str(tmp_path), lambda m: logged.append(m))

    class _DeadProc:
        def wait(self, timeout=None):
            return 1

        def poll(self):
            return 1

    acc.proc = _DeadProc()
    acc._watchdog(acc.proc)
    assert all("proxy" not in (m or "").lower() for m in logged), \
        "watchdog must not touch the system proxy"


# ---------------------------------------------------------------------------
# 6. file transfer: Unicode names + real-format detection
# ---------------------------------------------------------------------------

def test_sanitize_name_keeps_chinese():
    assert ft.sanitize_name("WeChat photo _20240101123456.jpg") == \
        "WeChat photo _20240101123456.jpg"
    assert ft.sanitize_name("C:\\Users\\x\\Pictures\\photo.png") == "photo.png"
    assert ft.sanitize_name("a/b/c.txt") == "c.txt"
    # windows-invalid characters are still stripped
    assert ft.sanitize_name('bad:name*?"<>.txt') == "badname.txt"
    # an empty result no longer fabricates a .bin
    assert ft.sanitize_name("???") == "file"


def test_sniff_extension_detects_real_formats(tmp_path):
    cases = [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, ".jpg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, ".png"),
        (b"GIF89a" + b"\x00" * 32, ".gif"),
        (b"%PDF-1.7\n" + b"\x00" * 32, ".pdf"),
        (b"PK\x03\x04" + b"\x00" * 32, ".zip"),
        (b"\x1f\x8b\x08" + b"\x00" * 32, ".gz"),
        (b"ID3\x03\x00" + b"\x00" * 32, ".mp3"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16, ".webp"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, ".mp4"),
        (b"OggS\x00\x02" + b"\x00" * 32, ".ogg"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 32, ".mkv"),
    ]
    for idx, (payload, expected) in enumerate(cases):
        p = tmp_path / ("f%d" % idx)
        p.write_bytes(payload)
        assert ft.sniff_extension(str(p)) == expected, payload[:8]
    p = tmp_path / "unknown"
    p.write_bytes(b"\x00" * 40)
    assert ft.sniff_extension(str(p)) == ""


def test_with_real_extension_renames_generic_names(tmp_path):
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x11" * 64
    p = tmp_path / "IMG_2024"
    p.write_bytes(jpeg)
    final = ft._with_real_extension(str(p))
    assert final.endswith(".jpg")
    assert os.path.exists(final)

    p2 = tmp_path / "received.bin"
    p2.write_bytes(jpeg)
    final2 = ft._with_real_extension(str(p2))
    assert final2.endswith("received.jpg")
    assert os.path.exists(final2)

    p3 = tmp_path / "keep.png"
    p3.write_bytes(jpeg)
    assert ft._with_real_extension(str(p3)) == str(p3)


# ---------------------------------------------------------------------------
# 7. receiver: directory pick, multi-file, wildcard bind
# ---------------------------------------------------------------------------

def _arm_receiver(tmp_path, save):
    return ft.ReceiverServer("127.0.0.1", USER, PASS, str(save))


def test_target_path_uses_the_sender_name_in_a_directory(tmp_path):
    save = tmp_path / "incoming"          # a DIRECTORY (new picker)
    save.mkdir()
    recv = _arm_receiver(tmp_path, save)
    assert recv._save_dir == str(save)
    target = recv._target_path("photo_2024.jpg")
    assert target == str(save / "photo_2024.jpg")


def test_target_path_still_accepts_a_legacy_file_path(tmp_path):
    save = tmp_path / "out.bin"
    recv = _arm_receiver(tmp_path, save)
    assert recv._save_dir == str(tmp_path)
    assert recv._target_path("photo.jpg") == str(tmp_path / "photo.jpg")


def test_receiver_stays_armed_for_several_files(tmp_path, paired):
    """beta1.0.61: after a completed transfer the listener keeps waiting —
    the old one-shot behaviour made the 2nd file ECONNREFUSED."""
    save = tmp_path / "incoming"
    save.mkdir()
    recv = _arm_receiver(tmp_path, save)
    recv.start()
    try:
        for round_no in range(2):
            payload = os.urandom(64 * 1024)
            src = tmp_path / ("src%d.bin" % round_no)
            src.write_bytes(payload)
            s = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
            try:
                s.settimeout(10)
                ft._send_line(s, "AUTH %s %s" % (USER, PASS))
                assert ft._recv_line(s) == "OK"
                name_b64 = base64.b64encode(b"photo.jpg").decode()
                ft._send_line(s, "OFFER %s %d %s" % (
                    name_b64, len(payload),
                    hashlib.sha256(payload).hexdigest()))
                assert ft._recv_line(s) == "ACCEPT"
                s.sendall(payload)
                assert ft._recv_line(s) == "DONE"
            finally:
                s.close()
            # the receiver must still be listening for the NEXT file
            probe = socket.create_connection(("127.0.0.1", ft.FT_PORT),
                                             timeout=5)
            probe.close()
    finally:
        recv.stop()


def test_received_photo_keeps_its_real_format(tmp_path, paired):
    """End-to-end: the phone sends a JPEG whose name has no extension."""
    jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" +
            os.urandom(200 * 1024))
    save = tmp_path / "incoming"
    save.mkdir()
    recv = _arm_receiver(tmp_path, save)
    recv.start()
    try:
        s = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
        try:
            s.settimeout(15)
            ft._send_line(s, "AUTH %s %s" % (USER, PASS))
            assert ft._recv_line(s) == "OK"
            name_b64 = base64.b64encode("IMG_20240101".encode()).decode()
            ft._send_line(s, "OFFER %s %d %s" % (
                name_b64, len(jpeg), hashlib.sha256(jpeg).hexdigest()))
            assert ft._recv_line(s) == "ACCEPT"
            s.sendall(jpeg)
            assert ft._recv_line(s) == "DONE"
        finally:
            s.close()
        target = save / "IMG_20240101.jpg"
        assert target.exists(), "the JPEG must land with its real extension"
        assert target.read_bytes() == jpeg
    finally:
        recv.stop()


# ---------------------------------------------------------------------------
# 6. campus-network resilience (beta1.0.61)
#
# The reporter is on a campus link: the subscription's nodes all sit on
# non-standard ports (10001-15005) which such networks silently drop, and
# airport DNS sections route lookups through DoH that is only reachable
# via the (dead) node. Symptoms seen in the wild: every dial ends in
# "i/o timeout", DIRECT traffic fails DNS with "context deadline exceeded",
# and the first store click hangs although other nodes in the same
# subscription are fine.
# ---------------------------------------------------------------------------

CAMPUS_CLASH = """\
mixed-port: 7890
mode: rule
dns:
  enable: true
  enhanced-mode: fake-ip
  nameserver: [1.1.1.1, 8.8.8.8]
  proxy-server-nameserver: ['https://cn.example-doh.net/dns-query/abc']
sniffer:
  enable: true
proxies:
  - { name: 'HK 01', type: vless, server: node.example.com, port: 10001,
      uuid: 11111111-2222-3333-4444-555555555555, network: tcp, tls: true }
  - { name: 'HK 02', type: vless, server: node.example.com, port: 10002,
      uuid: 11111111-2222-3333-4444-555555555555, network: tcp, tls: true }
proxy-groups:
  - name: "Airport Pick"
    type: select
    proxies:
      - HK 01
      - HK 02
rules:
  - MATCH,Airport Pick
"""


def test_generated_config_has_campus_safe_dns():
    """The subscription's dns:/sniffer: sections must NOT survive, and the
    injected DNS must resolve node domains LOCALLY (domestic plain IPs) —
    otherwise a blocked node takes every DNS lookup down with it."""
    cfg = store_accel._build_config_from_user_yaml(
        CAMPUS_CLASH, "rule", "Airport Pick")
    # the airport's DoH proxy-server-nameserver is gone
    assert "example-doh.net" not in cfg
    assert "sniffer" not in cfg
    # ours is in, exactly once, and never points through a proxy
    assert cfg.count("dns:") == 1
    dns = cfg[cfg.index("dns:"):]
    assert "223.5.5.5" in dns and "119.29.29.29" in dns
    assert "proxy-server-nameserver" in dns
    assert "https://" not in dns            # no DoH at all
    # Windows connectivity probes stay real -> no phantom "no Internet"
    assert "msftconnecttest" in dns and "msftncsi" in dns


def test_global_mode_drops_subscription_rules():
    cfg = store_accel._build_config_from_user_yaml(
        CAMPUS_CLASH, "global", "Airport Pick")
    assert "mode: global" in cfg
    # mihomo ignores rules in global mode; dead weight is dropped
    assert "\nrules:" not in cfg
    assert 'name: "Airport Pick"' in cfg


def test_node_endpoints_parsing():
    eps = store_accel._node_endpoints(CAMPUS_CLASH)
    assert eps == {"HK 01": ("node.example.com", 10001),
                   "HK 02": ("node.example.com", 10002)}
    # block-style configs parse too
    block = ("proxies:\n"
             "  - name: Block Node\n"
             "    type: ss\n"
             "    server: b.example.com\n"
             "    port: 8388\n")
    assert store_accel._node_endpoints(block) == {
        "Block Node": ("b.example.com", 8388)}
    # pseudo-proxies are not nodes
    assert store_accel._node_endpoints(
        "proxies:\n  - { name: DIRECT, type: direct }\n") == {}


def test_probe_tcp_classifies_blocked_vs_dead(monkeypatch):
    """timeout (filtered port — the campus symptom), refused (dead node),
    dns (unresolvable) and ok must be distinguishable.

    The classification is unit-tested against a fake socket layer because a
    real sandbox/VPN network stack answers every connect differently
    (some transparent layers even fake-accept TEST-NET addresses), which
    would make the assertions environment-dependent. One real listener
    still proves the ok path end to end.
    """
    import socket as _socket

    class _FakeSock(object):
        def __init__(self, *args):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            host = addr[0]
            if host == "10.1.1.1":            # filtered: no answer
                raise _socket.timeout()
            if host == "10.2.2.2":            # node down
                raise ConnectionRefusedError(111, "refused")
            return None                       # 10.3.3.3 accepts

        def close(self):
            pass

    monkeypatch.setattr(_socket, "socket", _FakeSock)
    monkeypatch.setattr(_socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", (h, p))])

    assert store_accel._probe_tcp("10.1.1.1", 10003)[0] == "timeout"
    status, detail = store_accel._probe_tcp("10.1.1.1", 10003)
    assert "屏蔽" in detail or "超时" in detail
    assert store_accel._probe_tcp("10.2.2.2", 443)[0] == "refused"
    assert store_accel._probe_tcp("10.3.3.3", 443)[0] == "ok"

    # unresolvable name -> dns (real resolver, .invalid is reserved)
    monkeypatch.undo()
    status, _ = store_accel._probe_tcp("no-such-host.invalid", 443,
                                       timeout=2)
    assert status == "dns"

    # and a REAL listener reports ok through the real socket path
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        status, _ = store_accel._probe_tcp("127.0.0.1", srv.getsockname()[1],
                                           timeout=2)
        assert status == "ok"
    finally:
        srv.close()


def test_dial_error_classification():
    t = store_accel._classify_dial_error
    # the exact shape the reporter's mihomo logged on campus
    hint = t('level=warning msg="[TCP] dial XFLTD (match Match/) '
             '127.0.0.1:7204 --> api.ip.sb:443 error: '
             '0af4430.cnrcz.cn:10003 connect error: context deadline '
             'exceeded"')
    assert "超时" in hint and "校园网" in hint
    hint = t('... dial tcp 13.214.167.107:10003: i/o timeout')
    assert "超时" in hint
    hint = t("... connection refused")
    assert "拒绝" in hint
    assert t("some unrelated info line") == ""


def test_probe_nodes_autoselects_a_reachable_node(tmp_path, monkeypatch):
    """With one reachable and one blocked node the group must switch to the
    reachable one before the system proxy is turned on."""
    acc = store_accel.StoreAccel(str(tmp_path), lambda *_a: None)
    live = socket.socket()
    live.bind(("127.0.0.1", 0))
    live.listen(1)
    live_port = live.getsockname()[1]
    cfg_dir = tmp_path / "mihomo"      # StoreAccel.workdir = <dir>/mihomo
    cfg_dir.mkdir()
    # a bound-then-closed loopback port: refused on a normal stack, a
    # timeout under a sandbox/transparent layer — either way "not ok",
    # and it always finishes inside the join window (unlike a name that
    # makes the resolver hang).
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    cfg = cfg_dir / "config.yaml"
    cfg.write_text(
        "proxies:\n"
        "  - { name: 'Blocked', type: vless, server: 127.0.0.1, "
        "port: %d }\n"
        "  - { name: 'Live', type: vless, server: 127.0.0.1, port: %d }\n"
        % (dead_port, live_port), encoding="utf-8")

    selected = {}

    class _Fake(object):
        @staticmethod
        def is_running():
            return True

        @staticmethod
        def _group_nodes():
            return ["Blocked", "Live"]

        @staticmethod
        def _group_name():
            return "Airport Pick"

        @staticmethod
        def current_node():
            return "Blocked"

        @staticmethod
        def select_node(name):
            selected["name"] = name
            return True, name

    for name in ("is_running", "_group_nodes", "_group_name",
                 "current_node", "select_node"):
        monkeypatch.setattr(acc, name, getattr(_Fake, name))
    try:
        results = acc.probe_nodes(timeout=2.0, autoselect=True)
        assert results["Live"][0] == "ok"
        assert results["Blocked"][0] in ("dns", "timeout", "refused")
        assert selected.get("name") == "Live", \
            "the blocked first node must not stay selected"
    finally:
        live.close()
