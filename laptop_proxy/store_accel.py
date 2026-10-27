# -*- coding: utf-8 -*-
r"""beta1.0.57: Store accelerator (Steam / Epic) — Mihomo as a separate helper
process.

Design constraints (deliberate):
- **No nodes are bundled, distributed or fetched by LinkHelm.** The user
  imports their OWN subscription URL or config file; it is stored locally
  only and never shipped with the app. (Same policy as FlClash / Clash
  Verge: the kernel is generic, the user brings the config.)
- **Mihomo runs as an UNMODIFIED external binary in a separate process**
  (GPL-3.0). It is never linked or compiled into LinkHelm, so the GPL does
  not extend to LinkHelm (MIT) — see THIRD_PARTY_NOTICES.md for the source
  link required by the license.
- **GRAFT ONLY — no WLAN, no Wi-Fi, no adapter, no route, ever.** The
  accelerator never creates/disables/renames/reconfigures a network
  adapter, never runs netsh/pnputil/Wlan*, never touches a WLAN profile or
  the Wi-Fi Direct group, and never changes a route or a metric. Its ENTIRE
  system surface is (a) one mihomo.exe child process bound to 127.0.0.1
  only, and (b) the WinINET system-proxy values under
  HKCU\...\Internet Settings — which are captured before the change and
  restored on stop, on crash (watchdog) and at exit (atexit). Nothing else
  in the machine is aware the accelerator exists.
- **Rule mode is an absolute graft**: ONLY the store/community domains may
  leave via the node; EVERYTHING else is forced DIRECT. The subscription's
  own rules are deliberately discarded — airport configs end with
  `MATCH,<node>`, which would push the whole PC through the node and make a
  dead node look like a total network outage. Global mode (all traffic via
  the node) exists as an explicit, labelled choice.
- beta1.0.63: COEXISTENCE, NOT GRAFTING. The accelerator never touches the
  WinINET system proxy, the DNS settings, the hosts file, the adapters or
  the routes — Clash Verge (or any other proxy) keeps full ownership of
  those. LinkHelm's kernel is a pure LOOPBACK micro-kernel: one mixed port
  on 127.0.0.1 the user (or another client) can opt into, nothing more.
  That is what makes "Clash Verge + LinkHelm at the same time" a non-event.
- Rule set covers the store/community domains of Steam and Epic only;
  everything else stays DIRECT (rule mode, the default). A global mode is
  offered as well — then ALL traffic follows the selected node (TCP always,
  UDP wherever the client speaks SOCKS5) — plus a node picker driven by
  mihomo's loopback REST API. The user's node choice is never hard-coded.
"""

import atexit
import hashlib
import os
import subprocess
import sys
import threading
import urllib.parse
import time

MIHOMO_VERSION = "Mihomo Meta v1.19.33 (MetaCubeX official release)"
MIHOMO_SHA256 = "1cde41cfffd3de0aedad63c0a8515e1ce5eea507e8133aed0f637d316c93800a"
# beta1.0.63: prefer 7897 — the number users recognise from Clash Verge /
# the xfltd panel screenshot (127.0.0.1:7897). When Clash Verge actually
# holds the port, start() falls back via _free_port, so coexistence with a
# running Clash Verge is unaffected (we simply take the next free port).
MIXED_PORT = 7897
# Local REST API of the mihomo helper (loopback only, empty secret) — used to
# list the subscription's nodes and to switch the active one. Never exposed
# beyond 127.0.0.1.
API_PORT = 9097
API_BASE = "http://127.0.0.1:%d" % API_PORT
# Selector group per mode: rule mode routes only the store domains through
# the STORE group; global mode routes everything through mihomo's GLOBAL
# group (TCP always, UDP wherever the client speaks SOCKS5).
GROUP_RULE = "STORE"
GROUP_GLOBAL = "GLOBAL"

# mihomo synthesises a placeholder proxy with this name when a
# proxy-provider's content cannot be parsed into real nodes (e.g. a
# v2ray/base64 subscription fed to a Clash kernel). Selecting it silently
# breaks every store request — treat it as "no nodes".
PLACEHOLDER_PROXIES = {"COMPATIBLE", "DIRECT", "REJECT", "PASS", "BLOCK"}

# Keys the accelerator manages itself; any copy found in a pasted/local
# Clash config is stripped before our values are injected (duplicate YAML
# keys make mihomo refuse to start).
_MANAGED_YAML_KEYS = (
    "mixed-port", "port", "socks-port", "allow-lan", "bind-address",
    "mode", "log-level", "external-controller", "external-controller-cors",
    "secret", "external-ui",
)

# Store/community/CDN-host domains ONLY. Game traffic and everything else
# stays DIRECT (no GEOIP database download needed — plain domain rules).
STEAM_DOMAINS = (
    "steampowered.com",      # store / api / help
    "steamcommunity.com",    # community / market
    "steamstatic.com",       # store static assets
    "steam-chat.com",        # chat
    "steamserver.net",       # store CDN edge selection
)
EPIC_DOMAINS = (
    "epicgames.com",         # store / launcher
    "epicgames.dev",         # storefront assets
    "unrealengine.com",      # launcher services
)


def find_mihomo_exe():
    """Locate the bundled mihomo.exe (frozen: _MEIPASS/mihomo, dev: ./mihomo).
    Returns (path, None) or (None, reason)."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "mihomo", "mihomo.exe"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "mihomo", "mihomo.exe"))
    for path in candidates:
        if os.path.isfile(path):
            digest = _sha256_file(path)
            if digest != MIHOMO_SHA256:
                return None, ("mihomo.exe hash mismatch (%s...); refused to run a "
                              "replaced binary" % digest[:12])
            return path, None
    return None, "mihomo.exe not found next to the app (mihomo/ folder missing)"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fin:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_config_text(sub_url, mode="rule"):
    """Build a minimal config for the store accelerator.

    `sub_url` must be an http(s) subscription handled by mihomo's
    proxy-provider (health-checked). Store domains -> STORE group,
    everything else -> DIRECT.

    `mode`:
      - "rule"   (default): only the store/community domains go through the
                 proxy; game downloads and launches stay DIRECT.
      - "global": ALL traffic goes through the selected node (TCP always;
                 UDP wherever the client uses the SOCKS5 mixed port).
    A loopback external-controller is added so the UI can list/switch nodes.
    """
    if mode not in ("rule", "global"):
        mode = "rule"
    domain_rules = "".join(
        "  - DOMAIN-SUFFIX,%s,STORE\n" % d for d in STEAM_DOMAINS + EPIC_DOMAINS
    )
    # NOTE: yaml written by hand (no PyYAML dependency); values are fixed
    # constants and the url is yaml-quoted, so no escaping surprises.
    return (
        "# Generated by LinkHelm store accelerator (user-supplied subscription).\n"
        "# No nodes are bundled with LinkHelm; this file references YOUR provider.\n"
        "mixed-port: %d\n"
        "allow-lan: false\n"
        "bind-address: 127.0.0.1\n"
        "mode: %s\n"
        "log-level: warning\n"
        "unified-delay: true\n"
        "external-controller: 127.0.0.1:%d\n"
        "secret: \"\"\n"
        "profile:\n"
        "  store-selected: true\n"
        "dns:\n"
        "  enable: true\n"
        "  nameserver:\n"
        "    - 223.5.5.5\n"
        "    - 119.29.29.29\n"
        "proxy-providers:\n"
        "  sub:\n"
        "    type: http\n"
        "    url: \"%s\"\n"
        "    interval: 86400\n"
        "    path: ./providers/sub.yaml\n"
        "    health-check:\n"
        "      enable: true\n"
        "      url: https://www.gstatic.com/generate_204\n"
        "      interval: 300\n"
        "proxy-groups:\n"
        "  - name: STORE\n"
        "    type: select\n"
        "    use:\n"
        "      - sub\n"
        "rules:\n"
        "%s"
        "  - MATCH,DIRECT\n" % (MIXED_PORT, mode, API_PORT, sub_url.strip(),
                                domain_rules)
    )


# ---------------------------------------------------------------------------
# Clash subscription handling (beta1.0.59).
#
# Real-world subscriptions come in several shapes and ALL of them must work:
#   1. an http(s) link whose body is a full Clash/Mihomo YAML config
#      (proxies + proxy-groups + rules)                       — the norm
#   2. an http(s) link whose body is a BASE64-encoded Clash config
#      (what most panels' "copy" button gives you)
#   3. a Clash YAML pasted straight into the entry box
#   4. a path to a local .yaml/.yml file
# The kernel's own proxy-provider only copes with a bare node list, which is
# why it used to degrade into a single "COMPATIBLE" placeholder. Instead the
# config is downloaded/read once, checked for a parseable node list, our
# managed keys are stripped and ours injected, and the store rules point at
# the user's own selector group.
# ---------------------------------------------------------------------------

def _strip_managed_keys(text):
    import re
    lines = []
    for line in text.splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line else None
        if key in _MANAGED_YAML_KEYS and not line[:1].isspace():
            continue
        lines.append(line)
    return "\n".join(lines)


def _yaml_section(text, name):
    """Top-level YAML mapping section body (None when absent)."""
    import re
    m = re.search(r"(?ms)^%s:[ \t]*\n(.*?)(?=^\S|\Z)" % re.escape(name), text)
    return m.group(1) if m else None


def _first_select_group(text):
    """Name of the first `type: select` group in proxy-groups ("" if none)."""
    import re
    section = _yaml_section(text, "proxy-groups")
    if not section:
        return ""
    for chunk in re.split(r"(?m)^  - ", section):
        if not chunk.strip():
            continue
        name = re.search(r"(?m)^\s*name:\s*[\"']?([^\"'\n,]+?)[\"']?\s*(?:,|$)", chunk)
        gtype = re.search(r"(?m)^\s*type:\s*[\"']?([^\"'\n]+?)[\"']?\s*(?:,|$)", chunk)
        if name and gtype and gtype.group(1).strip().lower() == "select":
            return name.group(1).strip()
    return ""


def _extract_proxy_items(text):
    """The proxies: entries as blocks (each block = list of lines, the first
    starting with '- '). Supports both flow-map and multi-line styles."""
    section = _yaml_section(text, "proxies")
    if not section:
        return []
    blocks, cur = [], []
    for line in section.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith("- "):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append(cur)
    out = []
    for block in blocks:
        first = block[0].strip()
        rest = [l[2:] if l.startswith("  ") else l for l in block[1:]]
        out.append([first] + rest)
    return out


def _node_endpoints(text):
    """{node name: (server, port)} for every proxies: entry.

    Supports the flow-map style airports emit
    (`- { name: 'X', type: vless, server: h, port: N, ... }`) and the
    block style. Used by the TCP reachability probe — on a campus network
    a plain connect is the difference between "port blocked" (timeout)
    and "node dead" (refused), which is exactly the diagnosis the user
    needs. Names of pseudo-proxies (DIRECT/REJECT/...) are skipped.
    """
    import re
    out = {}
    for block in _extract_proxy_items(text):
        # join with newlines, not spaces: a block-style node's name value
        # then ends at the line break instead of swallowing every key that
        # follows on subsequent lines.
        joined = "\n".join(block)
        nm = re.search(r"name:\s*[\"']?([^\"'\n,\}]+)", joined)
        if not nm:
            continue
        name = nm.group(1).strip()
        if name.upper() in PLACEHOLDER_PROXIES:
            continue
        srv = re.search(r"\bserver:\s*[\"']?([\w\.\-]+)", joined)
        prt = re.search(r"\bport:\s*(\d+)", joined)
        if srv and prt:
            out[name] = (srv.group(1), int(prt.group(1)))
    return out


def _probe_tcp(server, port, timeout=3.0):
    """Plain TCP connect to a node endpoint.

    Returns (status, detail) with status in:
      ok       — the host:port accepted a connection
      timeout  — nothing answered: the IP/port is filtered away, the
                 classic campus-network symptom (only 80/443 allowed)
      refused  — the host answered but rejected: node down / wrong port
      dns      — the hostname did not resolve
    A connect says nothing about the proxy protocol itself; it is a
    reachability verdict, and that is what fails first on restricted
    networks.
    """
    import socket
    import time
    try:
        infos = socket.getaddrinfo(server, port, socket.AF_INET,
                                   socket.SOCK_STREAM)
    except Exception:
        return "dns", "域名无法解析"
    last = None
    for family, _t, _p, _f, sockaddr in infos[:2]:
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.time()
        try:
            s.connect(sockaddr)
            return "ok", "%.0f ms" % ((time.time() - t0) * 1000)
        except socket.timeout:
            last = ("timeout", "连接超时（端口/IP 被网络屏蔽）")
        except ConnectionRefusedError:
            last = ("refused", "连接被拒绝（节点未监听）")
        except OSError as exc:
            last = ("refused", str(exc))
        finally:
            s.close()
    return last if last else ("timeout", "连接超时（端口/IP 被网络屏蔽）")


def _free_port(preferred, span=200):
    """A loopback TCP port the accelerator can actually bind.

    The user very often already runs another Clash (Clash Verge on 7897,
    the external controller on 9090, ...). Hard-coding our preferred port
    made the helper die with "address already in use" and the UI show a
    bare "start failed" — exactly the "连不上" symptom. Probe the
    preferred port first, then walk upward until one binds. Binding here
    is momentary (the socket is closed at once) and loopback-only.
    """
    import socket
    for candidate in range(preferred, preferred + span):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def _classify_dial_error(line):
    """Map a mihomo stderr dial error to a Chinese, actionable hint.

    Returns "" for lines that carry no diagnosis. The same phrase is
    logged once per distinct cause, not once per failed request.
    """
    low = (line or "").lower()
    if "i/o timeout" in low or "context deadline exceeded" in low \
            or "deadline exceeded" in low:
        return ("节点连接超时——目标 IP/端口无响应。校园网/运营商常屏蔽非标准"
                "端口，可改选其他节点（若全部超时，商店页面将不可用，"
                "但不影响其他网络）")
    if "connection refused" in low:
        return "节点拒绝连接——该节点可能已失效，请改选其他节点"
    if "no route to host" in low:
        return "无法路由到节点——网络不可达，请改选其他节点"
    if "token is error" in low or "403" in low:
        return "机场面板拒绝了订阅（token 失效/已用过/入口 IP 被屏蔽）"
    return ""


def _has_nodes(text):
    if _extract_proxy_items(text):
        return True
    section = _yaml_section(text, "proxy-providers")
    return bool(section and section.strip())


def _panel_error_text(html):
    """Pull the human-readable message out of a panel error page.

    Laravel-style panels (the ones behind most `/get?token=` subscription
    links) answer with a full HTML error page whose body carries the real
    reason — e.g. "token is error". Surfacing it beats a bare "403".

    Real-world panels differ in where they put the message: some use <p>,
    some only <title>, and the cctv/xfltd family puts it in a bare <div>
    next to the status code. So collect candidates from every text-bearing
    block element and keep the most specific one.
    """
    import re
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    candidates = []
    for tag in ("p", "title", "div", "h1", "h2", "h3", "span", "td",
                "strong", "li"):
        for m in re.finditer(r"(?is)<%s[^>]*>(.*?)</%s>" % (tag, tag),
                             text):
            msg = re.sub(r"(?s)<[^>]+>", "", m.group(1))
            msg = re.sub(r"\s+", " ", msg).strip()
            if 2 <= len(msg) <= 120:
                candidates.append(msg)
    # pure status codes / generic words carry no actionable information
    generic = {"forbidden", "error", "403", "404", "500", "401", "400",
               "unauthorized", "bad request", "not found", "forbidden."}
    specific = [c for c in candidates
                if c.lower() not in generic and not c.isdigit()]
    pool = specific or candidates
    if not pool:
        return ""
    # the most specific message is the longest non-generic one — e.g.
    # "token is error" over a bare "Forbidden" title.
    return max(pool, key=len)


# A desktop-Chrome header set. Panels behind a WAF routinely answer 403 to
# anything that does not look like a real browser — including the clash.meta
# user-agent — which is why a plain urllib GET used to fail.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",   # urllib cannot inflate gzip
    "Connection": "close",
}


def _unwrap_clash_link(link):
    """Unwrap a clash:// one-click import link to the inner subscription URL.

    The airport panels' "一键导入 Clash Verge" buttons emit
    `clash://install-config?url=<urlencoded>[&name=<name>][&...]` — that is
    the primary import path in xfltd's own Windows tutorial. Pasting that
    link verbatim used to land in the "unrecognised input" bucket, which is
    exactly the "导入识别不了" report. Returns (url, None) or (None, reason).
    """
    import urllib.parse
    text = (link or "").strip()
    if "?" not in text:
        return None, "链接缺少参数部分（?url=…）"
    scheme, _, query = text.partition("?")
    if scheme.lower() not in ("clash://install-config", "clash://install-config/"):
        # still try: any clash:// with a url= parameter
        if not scheme.lower().startswith("clash://"):
            return None, "不是 clash:// 链接"
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    url = (params.get("url") or [""])[0].strip()
    if not url:
        return None, "链接里没有 url 参数"
    if not url.lower().startswith(("http://", "https://")):
        return None, "url 参数不是 http(s) 订阅地址"
    return url, None


def _download_subscription(url, timeout=20, via_proxy=""):
    """Fetch a subscription the way a browser would. Returns (text, err).

    A failed fetch carries the panel's own message when it sent one, so the
    UI can say "token is error / 链接已过期" instead of a bare status code.
    beta1.0.63: the error string also carries the HTTP status and a short
    body preview, so "识别不了" always comes with evidence.

    via_proxy: optional "http://127.0.0.1:7897" — the user's running Clash
    client's local port. Some panels (xfltd observed 2026-10) BIND the
    subscription token to the IP that first fetched it, so a fetch must
    leave through the SAME exit as the reference client (Clash Verge) to be
    accepted; going through the user's Clash proxy reproduces that route.
    """
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        if via_proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": via_proxy, "https": via_proxy}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        reason = _panel_error_text(detail) or getattr(exc, "reason", "") \
            or ("HTTP %s" % exc.code)
        preview = ""
        if detail:
            flat = " ".join(detail.split())[:120]
            if flat and flat.lower() not in str(reason).lower():
                preview = "（页面返回：%s）" % flat
        return None, "HTTP %s %s%s" % (exc.code, reason, preview)
    except Exception as exc:
        return None, "download failed: %s" % exc


def _maybe_base64(text):
    """Decode a base64 subscription body (returns "" when not base64)."""
    import base64
    import re
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 16 or len(compact) % 4 != 0:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return ""
    try:
        raw = base64.b64decode(compact, validate=False)
    except Exception:
        return ""
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# v2ray-style subscription URIs -> Clash proxies.
#
# Most panels (…/get?token=… style links) hand out a BASE64 list of node
# URIs — ss://, vmess://, trojan://, vless://, hysteria2:// — which the
# mihomo kernel does NOT accept directly. Clash desktop clients convert it
# first; LinkHelm does the same, locally, in this function.
# ---------------------------------------------------------------------------

def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


def _b64d_flex(data):
    import base64
    text = data.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text)


def _split_hostport(hostport):
    hostport = hostport.strip()
    if hostport.startswith("["):  # IPv6 literal
        host, _, tail = hostport.partition("]")
        port = tail.lstrip(":")
        return host[1:], int(port or "0")
    host, _, port = hostport.rpartition(":")
    if not host:  # no colon at all — host without port
        return hostport, 0
    return host, int(port)


def _parse_ss_uri(rest):
    if "@" in rest:
        userinfo, _, hostport = rest.rpartition("@")
        cred = _b64d_flex(userinfo).decode("utf-8", "replace")
        method, _, password = cred.partition(":")
        host, port = _split_hostport(hostport)
    else:  # legacy whole-string base64: method:password@host:port
        dec = _b64d_flex(rest).decode("utf-8", "replace")
        cred, _, hostport = dec.rpartition("@")
        method, _, password = cred.partition(":")
        host, port = _split_hostport(hostport)
    return {"type": "ss", "server": host, "port": port,
            "cipher": method, "password": password, "udp": True}


def _parse_vmess_uri(rest):
    import json
    data = json.loads(_b64d_flex(rest).decode("utf-8", "replace"))
    if not isinstance(data, dict) or not data.get("add"):
        return None
    entry = {"type": "vmess", "server": str(data.get("add", "")),
             "port": int(data.get("port", 0) or 0),
             "uuid": str(data.get("id", "")),
             "alterId": int(data.get("aid", 0) or 0),
             "cipher": str(data.get("scy") or "auto"), "udp": True}
    if str(data.get("tls", "")).lower() in ("tls", "1", "true"):
        entry["tls"] = True
        if data.get("sni"):
            entry["servername"] = str(data.get("sni"))
    if data.get("ps"):
        entry["ps"] = str(data.get("ps"))
    net = str(data.get("net") or "tcp").lower()
    if net in ("ws", "h2", "http"):
        opts = {}
        if data.get("path"):
            opts["path"] = str(data.get("path"))
        headers = {}
        if data.get("host"):
            headers["Host"] = str(data.get("host"))
        if headers:
            opts["headers"] = headers
        entry["network"] = "ws" if net != "h2" else "h2"
        if opts:
            entry[("h2-opts" if net == "h2" else "ws-opts")] = opts
    elif net == "grpc":
        entry["network"] = "grpc"
        if data.get("path"):
            entry["grpc-opts"] = {"grpc-service-name": str(data.get("path"))}
    return entry


def _parse_trojan_uri(rest):
    import urllib.parse as up
    main, _, query = rest.partition("?")
    password, _, hostport = main.rpartition("@")
    host, port = _split_hostport(hostport)
    params = dict(up.parse_qsl(query))
    entry = {"type": "trojan", "server": host, "port": port,
             "password": up.unquote(password), "udp": True}
    if params.get("peer") or params.get("sni"):
        entry["sni"] = params.get("peer") or params.get("sni")
    if params.get("type") in ("ws", "grpc"):
        entry["network"] = params["type"]
        if params["type"] == "ws":
            opts = {}
            if params.get("path"):
                opts["path"] = up.unquote(params["path"])
            if params.get("host"):
                opts["headers"] = {"Host": up.unquote(params["host"])}
            if opts:
                entry["ws-opts"] = opts
        elif params.get("serviceName"):
            entry["grpc-opts"] = {"grpc-service-name": params["serviceName"]}
    return entry


def _parse_vless_uri(rest):
    import urllib.parse as up
    main, _, query = rest.partition("?")
    uuid, _, hostport = main.rpartition("@")
    host, port = _split_hostport(hostport)
    params = dict(up.parse_qsl(query))
    entry = {"type": "vless", "server": host, "port": port,
             "uuid": up.unquote(uuid), "udp": True}
    security = (params.get("security") or "none").lower()
    if security in ("tls", "reality", "xtls"):
        entry["tls"] = True
        if params.get("sni"):
            entry["servername"] = params["sni"]
        if security == "reality":
            ropts = {}
            if params.get("pbk"):
                ropts["public-key"] = params["pbk"]
            if params.get("sid"):
                ropts["short-id"] = params["sid"]
            if ropts:
                entry["reality-opts"] = ropts
                entry["client-fingerprint"] = params.get("fp") or "chrome"
    net = (params.get("type") or "tcp").lower()
    if net in ("ws", "h2", "http"):
        entry["network"] = "ws" if net != "h2" else "h2"
        opts = {}
        if params.get("path"):
            opts["path"] = up.unquote(params["path"])
        if params.get("host"):
            opts["headers"] = {"Host": up.unquote(params["host"])}
        if opts:
            entry[("h2-opts" if net == "h2" else "ws-opts")] = opts
    elif net == "grpc":
        entry["network"] = "grpc"
        if params.get("serviceName"):
            entry["grpc-opts"] = {"grpc-service-name": params["serviceName"]}
    return entry


def _parse_hysteria2_uri(rest):
    import urllib.parse as up
    main, _, query = rest.partition("?")
    auth, _, hostport = main.rpartition("@")
    host, port = _split_hostport(hostport)
    params = dict(up.parse_qsl(query))
    entry = {"type": "hysteria2", "server": host, "port": port,
             "password": up.unquote(auth)}
    if params.get("sni"):
        entry["sni"] = params["sni"]
    if params.get("insecure") in ("1", "true"):
        entry["skip-cert-verify"] = True
    if params.get("obfs") == "salamander" and params.get("obfs-password"):
        entry["obfs"] = "salamander"
        entry["obfs-password"] = up.unquote(params["obfs-password"])
    return entry


_URI_PARSERS = {
    "ss": _parse_ss_uri,
    "vmess": _parse_vmess_uri,
    "trojan": _parse_trojan_uri,
    "vless": _parse_vless_uri,
    "hysteria2": _parse_hysteria2_uri,
    "hy2": _parse_hysteria2_uri,
}

_PROXY_KEY_ORDER = (
    "name", "type", "server", "port", "password", "uuid", "cipher", "alterId",
    "udp", "tls", "servername", "skip-cert-verify", "network", "ws-opts",
    "h2-opts", "grpc-opts", "sni", "reality-opts", "client-fingerprint",
    "obfs", "obfs-password", "plugin", "plugin-opts",
)


def _render_proxy_block(entry):
    lines = []
    first = True
    for key in _PROXY_KEY_ORDER:
        if key not in entry:
            continue
        value = entry[key]
        prefix = "  - " if first else "    "
        first = False
        if isinstance(value, dict):
            lines.append("%s%s:" % (prefix, key))
            for sub_key, sub_val in value.items():
                if isinstance(sub_val, dict):
                    lines.append("      %s:" % sub_key)
                    for k3, v3 in sub_val.items():
                        lines.append("        %s: %s" % (k3, _yaml_scalar(v3)))
                else:
                    lines.append("      %s: %s" % (sub_key, _yaml_scalar(sub_val)))
        else:
            lines.append("%s%s: %s" % (prefix, key, _yaml_scalar(value)))
    return lines


def _uris_to_config_text(text):
    """Convert a subscription URI list into a Clash `proxies:` block.

    Returns "" when no supported URI is found. Unsupported schemes (ssr,
    http, …) are skipped; the caller decides what to do with the rest.
    """
    import urllib.parse as up
    entries = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if "://" not in line:
            continue
        scheme, _, rest = line.partition("://")
        scheme = scheme.lower()
        parser = _URI_PARSERS.get(scheme)
        if not parser:
            continue
        name = ""
        if "#" in rest:
            rest, _, frag = rest.partition("#")
            name = up.unquote(frag)
        try:
            entry = parser(rest)
        except Exception:
            entry = None
        if not entry or not entry.get("server") or not entry.get("port"):
            continue
        entry["name"] = name or entry.pop("ps", "") or \
            ("%s-%s:%d" % (scheme, entry["server"], entry["port"]))
        # duplicate names would make mihomo's config invalid
        base, n = entry["name"], 2
        while entry["name"] in seen:
            entry["name"] = "%s (%d)" % (base, n)
            n += 1
        seen.add(entry["name"])
        entries.append(entry)
    if not entries:
        return ""
    lines = ["proxies:"]
    for entry in entries:
        lines.extend(_render_proxy_block(entry))
    return "\n".join(lines) + "\n"


def _normalize_subscription_body(body):
    """Turn a downloaded/pasted subscription body into Clash config text.

    Accepts: a Clash/Mihomo YAML, a base64-encoded Clash YAML, or a base64/
    plain list of v2ray node URIs (converted locally). Returns "" when the
    format is unsupported.
    """
    if "proxies:" in body or "proxy-providers:" in body:
        return body
    decoded = _maybe_base64(body)
    if "proxies:" in decoded or "proxy-providers:" in decoded:
        return decoded
    converted = _uris_to_config_text(decoded or body)
    return converted


def _inline_providers(text, log=None):
    """Resolve http(s) proxy-providers LOCALLY and inline the nodes.

    Airports ship clash configs whose `proxy-providers:` point back at the
    panel URL. Letting mihomo fetch those itself fails on most panels — the
    kernel's user-agent gets a 403 (or the panel serves a format the kernel
    cannot parse), and the whole group degrades into the COMPATIBLE
    placeholder. Instead LinkHelm downloads each provider with a browser
    user-agent, converts the body, and inlines the resulting nodes; every
    `use: [provider]` reference is rewritten to the real node names. mihomo
    never talks to the panel at all.
    """
    import re
    section = _yaml_section(text, "proxy-providers")
    if not section:
        return text
    if "http://" not in section and "https://" not in section:
        return text

    def _render(items):
        out = []
        for blk in items:
            first, rest = blk[0], blk[1:]
            head = first[2:].strip() if first.startswith("- ") \
                else first.strip()
            out.append("  - " + head)
            for line in rest:
                out.append("    " + line.strip())
        return out

    existing = {n for n in (_block_name(b)
                            for b in _extract_proxy_items(text)) if n}
    inlined = []          # (provider_name, [node names])
    rendered = []         # proxies: lines to append
    for block in re.split(r"(?m)^  (?=[A-Za-z0-9_.-]+:[ \t]*$)", section):
        name_m = re.match(r"([A-Za-z0-9_.-]+):", block.strip())
        url_m = re.search(r"(?m)^\s*url:\s*[\"']?(https?://[^\"'\s]+)", block)
        if not name_m or not url_m:
            continue
        pname, purl = name_m.group(1), url_m.group(1)
        body, err = _download_subscription(purl)
        if body is None:
            if log:
                log("[accel] provider %s fetch failed: %s" % (pname, err))
            continue
        nodes_text = _normalize_subscription_body(body)
        if not _has_nodes(nodes_text):
            if log:
                log("[accel] provider %s returned no usable nodes" % pname)
            continue
        names = []
        for blk in _extract_proxy_items(nodes_text):
            nm = _block_name(blk)
            if not nm:
                continue
            base, n = nm, 2          # duplicate names break mihomo
            while nm in existing:
                nm = "%s (%d)" % (base, n)
                n += 1
            existing.add(nm)
            blk[0] = re.sub(r"name:\s*\"?[^\",}\"]+\"?",
                            "name: %s" % _yaml_scalar(nm), blk[0], count=1)
            names.append(nm)
            rendered.extend(_render([blk]))
        if names:
            inlined.append((pname, names))
            if log:
                log("[accel] provider %s inlined locally (%d nodes)"
                    % (pname, len(names)))
    if not inlined:
        return text

    # append the nodes to the top-level proxies: list
    joined = "\n".join(rendered)
    if _yaml_section(text, "proxies") is None:
        text = text.rstrip() + "\nproxies:\n" + joined + "\n"
    else:
        text = re.sub(r"(?ms)^proxies:[ \t]*\n",
                      "proxies:\n" + joined + "\n", text, count=1)

    # rewrite the groups: drop the provider from every use: list and give
    # the first affected select group the real node names
    gbody = _yaml_section(text, "proxy-groups")
    if gbody is not None:
        pnames = {p for p, _ in inlined}
        all_names = [n for _, ns in inlined for n in ns]

        def _strip_use(chunk):
            lines = chunk.splitlines()
            out, i = [], 0
            touched = False
            while i < len(lines):
                line = lines[i]
                if re.match(r"\s*use:\s*$", line):
                    j = i + 1
                    items = []
                    while j < len(lines) and re.match(r"\s+-\s+\S", lines[j]):
                        items.append(lines[j])
                        j += 1
                    kept = [it for it in items
                            if it.strip().lstrip("- ").strip().strip("\"'")
                            not in pnames]
                    if len(kept) != len(items):
                        touched = True
                    if kept:
                        out.append(line)
                        out.extend(kept)
                    i = j
                    continue
                out.append(line)
                i += 1
            return "\n".join(out), touched

        chunks = re.split(r"(?m)(?=^  - )", gbody)
        patched, given = [], False
        for chunk in chunks:
            if not chunk.strip():
                continue
            new, touched = _strip_use(chunk)
            if touched and not given and "type: select" in new:
                names_block = "".join("      - %s\n" % _yaml_scalar(n)
                                      for n in all_names)
                new = re.sub(r"(?m)^(    type:\s*select\s*)$",
                             r"\1\n    proxies:\n" + names_block,
                             new, count=1)
                given = True
            patched.append(new)
        text = re.sub(r"(?ms)^proxy-groups:[ \t]*\n.*?(?=^\S|\Z)",
                      "proxy-groups:\n" + "".join(patched), text, count=1)

    # nothing references the providers any more
    text = re.sub(r"(?ms)^proxy-providers:.*?(?=^\S|\Z)", "", text)
    return text


def _provider_names(text):
    """Names of top-level proxy-providers entries."""
    section = _yaml_section(text, "proxy-providers")
    if not section:
        return []
    import re
    return re.findall(r"(?m)^  ([A-Za-z0-9_.-]+):[ \t]*$", section)


def _block_name(block):
    """Node name of a proxies: block ('' when it has none).

    Strips the SAME surrounding quote char (single or double) the YAML used,
    via a backreference — airport node names are single-quoted (e.g.
    '🇭🇰 香港 01 [V]') and a naive " strip only leaves a stray leading quote,
    which then double-quotes into a name mihomo cannot match.
    """
    import re
    first = block[0].lstrip()
    if first.startswith("- {"):        # flow-map style: - {name: X, type: ss}
        first = first[1:]
    m = re.search(r"name:\s*([\"'])(.*?)\1\s*(?:,|}|$)", first)
    if m:
        return m.group(2).strip()
    m = re.search(r"name:\s*([^\s\"',}\n]+)", first)
    return m.group(1).strip() if m else ""


def _build_config_from_user_yaml(user_text, mode, group,
                                 mixed_port=MIXED_PORT, api_port=API_PORT):
    """Merge a user Clash config with the accelerator's managed keys.

    The user's proxies / proxy-groups / rules are preserved verbatim; only
    the ports, bind address, mode and the loopback API are ours. Store
    domains are routed at the TOP of the rule list so they always reach the
    selected node regardless of what the subscription's own rules do.
    """
    import re
    text = _strip_managed_keys(user_text)
    # A subscription's own dns:/sniffer:/tun: sections are dropped as well:
    # airport configs routinely point proxy-server-nameserver at a DoH
    # endpoint that is only reachable THROUGH the node, so on a network
    # where the node is blocked (campus networks drop non-standard ports)
    # every DNS lookup dies with "context deadline exceeded" — including
    # DIRECT traffic. We inject a campus-safe DNS block below instead.
    # tun: is dropped for a harder reason: mihomo WOULD create a virtual
    # adapter with it, and this accelerator must never touch one.
    for sec in ("dns", "sniffer", "tun"):
        text = re.sub(r"(?ms)^%s:[ \t]*\n.*?(?=^\S|\Z)" % sec, "", text)
    group = group or GROUP_RULE
    header = (
        "# Generated by LinkHelm store accelerator from YOUR Clash config.\n"
        "# No nodes are bundled with LinkHelm.\n"
        "mixed-port: %d\n"
        "allow-lan: false\n"
        "bind-address: 127.0.0.1\n"
        "mode: %s\n"
        "log-level: warning\n"
        "external-controller: 127.0.0.1:%d\n"
        # beta1.0.62: belt and braces. mihomo only creates a virtual
        # adapter when a tun: section says so — this one says the
        # opposite, in OUR hand-written config, after the subscription's
        # own tun: was stripped above.
        "tun:\n"
        "  enable: false\n"
        # Campus-safe DNS: domestic plain-IP nameservers only, and the node
        # domains themselves are resolved LOCALLY (proxy-server-nameserver)
        # so a blocked node can never take DNS down with it. fake-ip keeps
        # store domains on the machine; the filter keeps the Windows
        # connectivity probes (NCSI) real so the OS never decides the
        # machine lost its Internet because of us.
        "dns:\n"
        "  enable: true\n"
        "  ipv6: false\n"
        "  enhanced-mode: fake-ip\n"
        "  fake-ip-filter:\n"
        "    - +.lan\n"
        "    - +.local\n"
        "    - +.localhost\n"
        "    - +.msftconnecttest.com\n"
        "    - +.msftncsi.com\n"
        "  default-nameserver:\n"
        "    - 223.5.5.5\n"
        "    - 119.29.29.29\n"
        "  nameserver:\n"
        "    - 223.5.5.5\n"
        "    - 119.29.29.29\n"
        "  proxy-server-nameserver:\n"
        "    - 223.5.5.5\n"
        "    - 119.29.29.29\n"
        % (mixed_port, "global" if mode == "global" else "rule", api_port)
    )
    # No selector group in the config? Wrap the user's nodes in one that
    # points at either the proxies list or the providers.
    if not _first_select_group(text):
        items = _extract_proxy_items(text)
        providers = _provider_names(text)
        if items or providers:
            body = "proxy-groups:\n  - name: %s\n    type: select\n" % group
            if items:
                # a group's proxies: list holds the node NAMES, not the
                # node definitions themselves
                body += "    proxies:\n"
                for block in items:
                    nm = _block_name(block)
                    if nm:
                        body += "      - %s\n" % _yaml_scalar(nm)
            else:
                body += "    use:\n"
                body += "".join("      - %s\n" % p for p in providers)
            if _yaml_section(text, "proxy-groups") is None:
                text = text.rstrip() + "\n" + body
            else:  # replace an existing (non-select) proxy-groups block
                text = re.sub(r"(?ms)^proxy-groups:.*?(?=^\S|\Z)",
                              body, text)
        # else: nothing to wrap — the caller's _has_nodes() check rejects this
    if mode == "global":
        # global mode is the user's explicit, labelled choice: ALL traffic
        # follows the selected node. mihomo ignores the rules list entirely
        # in global mode, so the subscription's own rules (which end in
        # MATCH,<node> anyway) are dropped — no dead weight, no confusion.
        text = re.sub(r"(?ms)^rules:.*?(?=^\S|\Z)", "", text)
    else:
        # rule mode = GRAFT. ONLY the store/community domains may leave via
        # the node; EVERYTHING else stays on the local connection. The
        # subscription's own rules are deliberately dropped: airport configs
        # end with `MATCH,<node group>` (or GEOIP rules), which would send
        # the WHOLE PC through the node — a dead node then looks exactly
        # like "断网/卡死". With our rules the accelerator can never affect
        # anything but store pages.
        store_rules = "".join(
            "  - DOMAIN-SUFFIX,%s,%s\n" % (d, group)
            for d in STEAM_DOMAINS + EPIC_DOMAINS
        )
        text = re.sub(r"(?ms)^rules:.*?(?=^\S|\Z)", "", text)
        text = text.rstrip() + "\nrules:\n" + store_rules + "  - MATCH,DIRECT\n"
    return header + text.lstrip("\n")


# ---------------------------------------------------------------------------
# Read-only adapter census. GetAdaptersAddresses is a pure QUERY API — it
# cannot add, remove, rename or reconfigure anything (unlike netsh/pnputil,
# which this module never calls). The census exists to PROVE the isolation:
# the accelerator must find exactly the same adapters before and after it
# starts. TUN (131) / Wi-Fi (71) counts are reported explicitly so a
# regression shows up as a log line instead of a user's broken network.
#
# beta1.0.63: the WinINET system-proxy block that used to live here was
# REMOVED. Overwriting HKCU Internet Settings stole the proxy from the
# user's own Clash Verge (7897) — when the helper's kernel then had any
# problem, every WinINET client (including the OS connectivity probes)
# died with "无法访问 Internet" on ALL adapters. The accelerator is now a
# loopback-only micro-kernel and coexists with whatever proxy the user
# already runs; it never writes the registry, the hosts file, DNS, routes
# or adapters.
# ---------------------------------------------------------------------------

_IF_TYPE_TUNNEL = 131
# beta1.0.64: LinkHelm's own lwIP tunnel adapter ("LinkHelm-IwIP") is
# IF_TYPE_PROP_VIRTUAL. Counting it as well makes the census actually show
# whether a virtual adapter is present instead of a permanent "TUN 0".
_IF_TYPE_PROP_VIRTUAL = 53
_IF_TYPE_IEEE80211 = 71


def _list_adapters():
    """[(friendly name, if_type)] for every adapter, or [] when unavailable.

    Read-only: ctypes GetAdaptersAddressesW. No subprocess, no registry,
    no netsh — nothing here can change the machine.
    """
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        return []
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    except Exception:
        return []

    class _Addr(ctypes.Structure):
        pass

    _Addr._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(_Addr)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG),
        # beta1.0.64: IP_ADAPTER_ADDRESSES has a `Flags` DWORD between
        # PhysicalAddressLength and Mtu. Without it every later field shifts
        # by 4 bytes, so IfType actually read Mtu (1500) — the census then
        # reported "TUN 0 / Wi-Fi 0" on every machine and the isolation
        # proof (adapter count unchanged) could never fire.
        ("Flags", wintypes.ULONG),
        ("Mtu", wintypes.ULONG),
        ("IfType", wintypes.ULONG),
    ]

    flags = 0  # GAA_FLAG_SKIP_UNICAST etc. — plain list is fine here
    size = wintypes.ULONG(0)
    iphlpapi.GetAdaptersAddresses(wintypes.ULONG(0), wintypes.ULONG(flags),
                                  None, None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    rc = iphlpapi.GetAdaptersAddresses(wintypes.ULONG(0), wintypes.ULONG(flags),
                                       None, ctypes.cast(buf, ctypes.POINTER(_Addr)),
                                       ctypes.byref(size))
    if rc != 0:  # ERROR_SUCCESS
        return []
    out = []
    node = ctypes.cast(buf, ctypes.POINTER(_Addr))
    while node:
        entry = node.contents
        try:
            name = entry.FriendlyName or entry.Description or ""
        except Exception:
            name = ""
        out.append((str(name), int(entry.IfType)))
        node = entry.Next
    return out


def _adapter_census():
    """One-line summary of the current adapter landscape (or "")."""
    adapters = _list_adapters()
    if not adapters:
        return ""
    tun = [n for n, t in adapters
           if t in (_IF_TYPE_TUNNEL, _IF_TYPE_PROP_VIRTUAL)]
    wifi = [n for n, t in adapters if t == _IF_TYPE_IEEE80211]
    return ("网卡普查：共 %d 个（TUN %d：%s；Wi-Fi %d：%s）"
            % (len(adapters), len(tun), "/".join(tun) or "无",
               len(wifi), "/".join(wifi) or "无"))


def read_system_proxy():
    """Read-only: (enabled, server) currently set in WinINET.

    beta1.0.63: kept for DISPLAY only (the accel page shows whose proxy is
    live so the user can see Clash Verge still owns it). Nothing in this
    module ever WRITES the registry any more — that write path is what
    broke the user's network (it stole the proxy from their own Clash).
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except OSError:
                server = ""
        return bool(enabled), server or ""
    except Exception:
        return False, ""


def inspect_hosts():
    """Read-only hosts-file integrity report.

    Returns (path, bom_count, active_entries, size). A hosts file mangled
    by third-party "optimiser"/security tools shows up as stray UTF-8 BOM
    characters (U+FEFF) — 电脑管家 flags exactly that as "host 异常".
    LinkHelm never writes this file; this exists so the app can DIAGNOSE
    and (on an explicit user click) repair it.
    """
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                        "System32", "drivers", "etc", "hosts")
    try:
        with open(path, "rb") as fin:
            raw = fin.read()
    except Exception as exc:
        return path, -1, [], 0
    text = raw.decode("utf-8", "replace")
    bom = text.count("\ufeff")
    active = [ln.strip() for ln in text.splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    return path, bom, active, len(raw)


def repair_hosts():
    """User-initiated hosts repair: strip stray BOM characters ONLY.

    Conservative by design — it removes U+FEFF characters and nothing
    else (no entry is added, removed or reordered). Returns
    (ok, message). Needs the app's elevation, which LinkHelm already has.
    """
    path, bom, _active, _size = inspect_hosts()
    if bom < 0:
        return False, "无法读取 hosts 文件（权限不足？）"
    if bom == 0:
        return True, "hosts 文件正常，无需修复"
    try:
        with open(path, "rb") as fin:
            raw = fin.read()
        fixed = raw.replace(b"\xef\xbb\xbf", b"")
        with open(path, "wb") as fout:
            fout.write(fixed)
        return True, ("已清除 hosts 文件中 %d 个异常字符（BOM），"
                      "其余内容未动" % bom)
    except Exception as exc:
        return False, "修复失败：%s（请以管理员身份运行 LinkHelm）" % exc


class StoreAccel(object):
    """Lifecycle for the mihomo helper process. All methods are safe to call
    from any thread; UI reads the returned state strings."""

    def __init__(self, config_dir, log_fn):
        self.log = log_fn
        self.workdir = os.path.join(config_dir, "mihomo")
        self.proc = None
        # beta1.0.62: ports are chosen at start() — the user may already
        # run another Clash on our preferred numbers (Clash Verge: 7897
        # mixed / 9090 API). Until then they hold the preferred defaults.
        self.mixed_port = MIXED_PORT
        self.api_port = API_PORT
        self._log_tail = []          # last stderr lines for error reporting
        self._lock = threading.RLock()
        # beta1.0.63: NO system-proxy state exists any more. The kernel is
        # a loopback-only micro-kernel; it coexists with the user's own
        # Clash Verge (which keeps owning the WinINET proxy) instead of
        # stealing it. See the module docstring.
        pref = self.load_pref()
        self.mode = pref.get("mode", "rule")           # rule | global
        # selector group that actually holds the user's nodes: the first
        # `type: select` group of the imported Clash config ("" -> STORE).
        self.user_group = pref.get("userGroup", "")
        atexit.register(self.shutdown)

    # -- settings (accel.json next to settings.json; local-only) -----------

    def _pref_path(self):
        return os.path.join(os.path.dirname(self.workdir), "accel.json")

    def load_pref(self):
        import json
        try:
            with open(self._pref_path(), encoding="utf-8") as fin:
                return json.load(fin)
        except Exception:
            return {}

    def save_pref(self, **kwargs):
        import json
        data = self.load_pref()
        data.update(kwargs)
        try:
            os.makedirs(os.path.dirname(self._pref_path()), exist_ok=True)
            with open(self._pref_path(), "w", encoding="utf-8") as fout:
                json.dump(data, fout, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # -- state --------------------------------------------------------------

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def status_text(self):
        with self._lock:
            if self.is_running():
                node = self.current_node()
                return "running  127.0.0.1:%d%s" % (
                    self.mixed_port,
                    ("  |  %s" % node) if node else "")
            if self.proc is not None:
                tail = " ".join(self._log_tail[-3:])[-200:]
                return "stopped: %s" % (tail or "exit code %s" % self.proc.returncode)
            return ""

    # -- node selection (loopback REST API) ----------------------------------

    def _group_name(self):
        if getattr(self, "mode", "rule") == "global":
            return GROUP_GLOBAL
        return getattr(self, "user_group", "") or GROUP_RULE

    def _api(self, method, path, payload=None, timeout=4):
        """Minimal loopback REST call; returns (ok, parsed_json_or_text)."""
        import json
        import urllib.request
        import urllib.error
        url = "http://127.0.0.1:%d%s" % (self.api_port, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                return True, json.loads(body)
            except ValueError:
                return True, body
        except urllib.error.HTTPError as exc:
            return False, "HTTP %s" % exc.code
        except Exception as exc:
            return False, str(exc)

    def _group_nodes(self):
        """Real node names of the active selector group ([] on any failure).

        mihomo's COMPATIBLE placeholder (an unparseable subscription) and
        the built-in pseudo-proxies are filtered out — they are not nodes.
        """
        ok, data = self._api("GET", "/proxies")
        if not ok or not isinstance(data, dict):
            return []
        group = data.get("proxies", {}).get(self._group_name())
        if not isinstance(group, dict):
            return []
        out = []
        for n in group.get("all", []) or []:
            n = str(n)
            if n and n.upper() not in PLACEHOLDER_PROXIES:
                out.append(n)
        return out

    def list_nodes(self):
        """Node names offered by the active selector group ([] on failure)."""
        if not self.is_running():
            return []
        return self._group_nodes()

    def current_node(self):
        if not self.is_running():
            return ""
        ok, data = self._api("GET", "/proxies/%s" % self._group_name())
        if ok and isinstance(data, dict):
            return str(data.get("now") or "")
        return ""

    def select_node(self, name):
        """Switch the active node of the selector group. (ok, message)."""
        if not self.is_running():
            return False, "accelerator is not running"
        name = (name or "").strip()
        if not name:
            return False, "empty node name"
        ok, _ = self._api("PUT", "/proxies/%s" % self._group_name(),
                          {"name": name})
        if not ok:
            return False, "mihomo API rejected %s" % name
        self.save_pref(node=name)
        self.log("[accel] node -> %s (%s)" % (name, self._group_name()))
        return True, name

    # -- latency test / auto-select (Clash Verge style) ----------------------

    # The SAME endpoint Clash Verge's 延迟测试 button hits. A node answers
    # with {"delay": <ms>} or nothing when it errors — so the numbers shown
    # in LinkHelm are directly comparable with the reference client.
    # beta1.0.7: GitHub is the PRIMARY test target (user request) — it is
    # reliably reachable through healthy exit nodes, while Google's 204 is
    # filtered by many airports. The delay endpoint's timeout parameter is
    # 5000 ms: a node answering within 5 s shows its real milliseconds,
    # only a node with NO answer under 5 s is a 超时.
    DELAY_TEST_URL = "https://github.com"
    DELAY_TEST_TIMEOUT = 5.0
    DELAY_TEST_URLS = (
        "https://github.com",
        "https://api.github.com",
        "https://cp.cloudflare.com/generate_204",
    )

    def _delay_one(self, name, url, timeout):
        """(ms, error) for one node against one test URL. ms is None on error."""
        path = ("/proxies/%s/delay?timeout=%d&url=%s"
                % (urllib.parse.quote(name, safe=""),
                   int(timeout * 1000), urllib.parse.quote(url, safe="")))
        ok, data = self._api("GET", path, timeout=timeout + 3.0)
        if ok and isinstance(data, dict):
            try:
                return int(data.get("delay")), ""
            except (TypeError, ValueError):
                return None, (str(data.get("message") or data)[:60])
        return None, (str(data)[:60] if data else "请求失败")

    def test_delays(self, timeout=None, test_url=None):
        """Per-node HTTP latency via mihomo's own delay endpoint.

        beta1.0.7: timeout defaults to DELAY_TEST_TIMEOUT (5000 ms) — a node
        answering within 5 s is NOT a timeout, only a silent one is.

        Returns {name: ms or None}; None means every test URL failed — the
        node really is unreachable and the UI shows 超时. A node that answers
        any of DELAY_TEST_URLS gets its real milliseconds.

        Run from a worker thread — each call blocks until measured.
        """
        if timeout is None:
            timeout = self.DELAY_TEST_TIMEOUT
        if not self.is_running():
            return {}
        nodes = self._group_nodes()
        if not nodes:
            return {}
        urls = [test_url] if test_url else list(self.DELAY_TEST_URLS)
        results = {}
        lock = threading.Lock()

        def _one(name):
            ms = None
            last_err = ""
            for url in urls:
                ms, err = self._delay_one(name, url, timeout)
                if ms is not None:
                    break
                last_err = err
            with lock:
                results[name] = ms

        threads = [threading.Thread(target=_one, args=(n,), daemon=True)
                   for n in nodes[:60]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout * len(urls) + 6.0)
        return results

    def auto_select(self, timeout=None, test_url=None):
        """Pick the lowest-latency node and switch to it.

        Returns (name, ms, results); name == "" when nothing answered.
        """
        results = self.test_delays(timeout=timeout, test_url=test_url)
        best, best_ms = "", None
        for name, ms in results.items():
            if ms is not None and (best_ms is None or ms < best_ms):
                best, best_ms = name, ms
        if best:
            self.select_node(best)
        return best, best_ms, results

    # -- node reachability (campus-network diagnosis) ------------------------

    def probe_nodes(self, timeout=3.0, autoselect=True):
        """TCP-probe every node of the active group; report and auto-pick.

        Returns {name: (status, detail)} ({} when not running). This is the
        campus-network lifeline: a plain connect distinguishes
          timeout  -> the IP/port is filtered by the network (very common
                      on campus/operator links that only pass 80/443)
          refused  -> the node itself is down
          ok       -> reachable from here
        With autoselect the group switches to the first reachable node, so
        a subscription that starts on a blocked port stops being a dead
        end. Probing never touches the network stack beyond one TCP
        connect per node — no adapter, no route, no WLAN.
        """
        if not self.is_running():
            return {}
        nodes = self._group_nodes()
        if not nodes:
            return {}
        endpoints = {}
        try:
            cfg_path = os.path.join(self.workdir, "config.yaml")
            with open(cfg_path, encoding="utf-8", errors="replace") as fin:
                endpoints = _node_endpoints(fin.read())
        except Exception:
            endpoints = {}

        results = {}
        lock = threading.Lock()

        def _one(name):
            ep = endpoints.get(name)
            if not ep:
                with lock:
                    results[name] = ("unknown", "未找到节点地址")
                return
            status, detail = _probe_tcp(ep[0], ep[1], timeout)
            with lock:
                results[name] = (status, detail)

        threads = [threading.Thread(target=_one, args=(n,), daemon=True)
                   for n in nodes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 3.5)

        # beta1.0.64: a thread still inside getaddrinfo() (DNS resolution is
        # NOT bounded by the socket timeout) had written nothing, so every
        # unclassified node fell through to "无法判断" — that is exactly the
        # reported "节点探测：无法判断（20 个节点）" on a subscription that was
        # demonstrably working (Steam loaded fine). Anything unfinished is a
        # slow/filtered endpoint, i.e. a timeout, and is now reported as one.
        for n in nodes:
            if n not in results:
                results[n] = ("timeout", "探测未完成（DNS 解析或连接过慢）")

        ok = [n for n in nodes if results.get(n, ("",))[0] == "ok"]
        timeout_n = [n for n in nodes
                     if results.get(n, ("",))[0] == "timeout"]
        refused = [n for n in nodes
                   if results.get(n, ("",))[0] == "refused"]
        dns_n = [n for n in nodes if results.get(n, ("",))[0] == "dns"]
        unknown_n = [n for n in nodes if results.get(n, ("",))[0] == "unknown"]
        if ok:
            self.log("[accel] 节点探测：%d 个可连接，%d 个超时，%d 个被拒"
                     % (len(ok), len(timeout_n), len(refused)))
        elif timeout_n and not refused and not dns_n:
            self.log("[accel] 节点探测：全部 %d 个节点均超时——你的网络屏蔽了"
                     "这些节点的 IP/端口（校园网常见）。商店页面暂不可用，"
                     "但不影响其他网络" % len(timeout_n))
        elif refused:
            self.log("[accel] 节点探测：%d 个被拒、%d 个超时——节点本身可能"
                     "已失效，或网络阻断" % (len(refused), len(timeout_n)))
        elif dns_n:
            self.log("[accel] 节点探测：%d 个节点域名无法解析、%d 个超时——"
                     "本机 DNS 无法解析节点地址（可尝试更换 DNS）"
                     % (len(dns_n), len(timeout_n)))
        elif unknown_n:
            # beta1.0.64: say WHY instead of a bare "无法判断" — usually the
            # node address could not be read back out of the written config.
            self.log("[accel] 节点探测：%d 个节点未取到地址（%s）——无法探测，"
                     "不影响内核按规则转发" % (len(unknown_n),
                                              results[unknown_n[0]][1]))
        else:
            self.log("[accel] 节点探测：无法判断（%d 个节点）" % len(nodes))

        if autoselect and ok:
            current = self.current_node()
            if current not in ok:
                self.select_node(ok[0])
                self.log("[accel] 已自动切换到可连接节点：%s" % ok[0])
        return results

    # -- start / stop ---------------------------------------------------------

    def start(self, sub_url, mode="rule", clash_proxy=""):
        """Launch the helper with the user's own Clash subscription.

        Accepts (checked in this order):
          - a clash://install-config?url=... one-click link (the format the
            airport panels' "一键导入 Clash Verge" buttons emit — xfltd's
            tutorial is built around it; the inner URL is unwrapped)
          - an http(s) subscription link (Clash/Mihomo YAML, or base64 of it)
          - a path to a local .yaml/.yml Clash config
          - the Clash config text pasted straight into the entry box

        beta1.0.63: no system proxy is ever touched. The kernel binds one
        mixed port on 127.0.0.1 and nothing else — the user's own Clash
        Verge keeps owning the WinINET proxy, so both can run at once.
        """
        with self._lock:
            if self.is_running():
                return True, "already running"
            exe, err = find_mihomo_exe()
            if exe is None:
                return False, err
            if mode not in ("rule", "global"):
                mode = "rule"
            self.mode = mode
            # beta1.0.63: optional local Clash proxy used to fetch the
            # subscription ("127.0.0.1:7897" or a full http:// URL). Some
            # panels bind the token to the first fetching IP — fetching
            # through the user's running Clash reproduces that exit route.
            clash_proxy = (clash_proxy or "").strip()
            if clash_proxy and "://" not in clash_proxy:
                clash_proxy = "http://" + clash_proxy
            raw = (sub_url or "").strip()
            if not raw:
                return False, ("import a subscription URL or config.yaml first "
                               "(no nodes are bundled with LinkHelm)")
            os.makedirs(self.workdir, exist_ok=True)
            os.makedirs(os.path.join(self.workdir, "providers"), exist_ok=True)

            # ---- classify the input --------------------------------------
            # clash:// one-click / URL / local file / pasted text, in Clash
            # OR base64-URI form.
            cfg_text = None
            source = raw
            if raw.lower().startswith("clash://"):
                # 一键导入链接（xfltd 教程第 3 节的主推方式）：
                # clash://install-config?url=<urlencoded>&name=<name>
                unwrapped, uerr = _unwrap_clash_link(raw)
                if unwrapped is None:
                    return False, ("无法解析 clash:// 导入链接：%s\n"
                                   "请改用链接里的订阅地址本身"
                                   "（http/https 开头）" % uerr)
                self.log("[accel] clash:// one-click link unwrapped")
                raw = source = unwrapped
            if raw.lower().startswith(("http://", "https://")):
                # beta1.0.63: try DIRECT first; when it fails (GFW reset OR
                # an IP-bound panel), retry through the user's own Clash
                # client so the request exits where Clash Verge's import
                # did — that is the route the panel's token binding knows.
                body, derr = _download_subscription(raw)
                if body is None and clash_proxy:
                    self.log("[accel] direct fetch failed (%s); retrying "
                             "via Clash proxy %s" % (derr, clash_proxy))
                    body, derr = _download_subscription(raw,
                                                        via_proxy=clash_proxy)
                if body is None:
                    hint = ""
                    low = (derr or "").lower()
                    if "403" in low or "forbidden" in low or \
                            "token" in low or "error" in low:
                        hint = ("\n——订阅被机场拒绝（token 过期/已用过/需登录）。"
                                "请到机场后台重新生成订阅链接；"
                                "若机场把订阅绑定到 IP，请在「Clash 代理」里"
                                "填本机 Clash 端口（如 127.0.0.1:7897）后重试；"
                                "或点「粘贴」把内容直接粘到输入框")
                    return False, "订阅下载失败：%s%s" % (derr, hint)
                cfg_text = _normalize_subscription_body(body)
                if not _has_nodes(cfg_text):
                    return False, ("该订阅没有返回任何可用节点（既不是 "
                                   "Clash/Mihomo 配置，也无法解析出 ss/vmess/"
                                   "trojan/vless 节点）——请确认订阅格式")
                self.log("[accel] subscription downloaded (%d bytes)"
                         % len(body))
            elif raw.lower().endswith((".yaml", ".yml")) and os.path.isfile(raw):
                with open(raw, "r", encoding="utf-8", errors="replace") as fin:
                    cfg_text = _normalize_subscription_body(fin.read())
                if not _has_nodes(cfg_text):
                    return False, ("所选文件中找不到 proxies: 节点列表或 "
                                   "proxy-providers:")
                self.log("[accel] using local config file: %s" % raw)
            else:
                cfg_text = _normalize_subscription_body(raw)
                if not _has_nodes(cfg_text):
                    return False, ("无法识别输入。支持：① 机场的「一键导入」"
                                   "clash:// 链接或订阅地址（http/https）"
                                   "② 本地 .yaml 配置路径 ③ 直接粘贴订阅内容"
                                   "（Clash 配置或 base64 节点列表）")

            # beta1.0.61: a clash config that references the panel through
            # proxy-providers is resolved HERE (browser UA) and inlined, so
            # the kernel never talks to the airport itself — panels 403 the
            # kernel's own user-agent, which used to leave only COMPATIBLE.
            cfg_text = _inline_providers(cfg_text, log=self.log)

            # beta1.0.62: pick ports the user's own Clash is NOT using.
            # Clash Verge sits on 7897 (mixed) / 9090 (API); a hard-coded
            # 7890/9097 used to collide and the helper died with a bare
            # "address already in use" — the "连不上" symptom.
            self.mixed_port = _free_port(MIXED_PORT)
            self.api_port = _free_port(API_PORT)
            if self.mixed_port != MIXED_PORT:
                self.log("[accel] port %d busy (another Clash?) — using %d"
                         % (MIXED_PORT, self.mixed_port))
            if self.api_port != API_PORT:
                self.log("[accel] API port %d busy — using %d"
                         % (API_PORT, self.api_port))

            # beta1.0.62: adapter census BEFORE the launch. The census is
            # read-only (GetAdaptersAddresses); it exists so the log can
            # PROVE the isolation — same adapters after the start as before.
            census_before = _adapter_census()
            if census_before:
                self.log("[accel] %s" % census_before)

            user_group = _first_select_group(cfg_text)
            final = _build_config_from_user_yaml(
                cfg_text, mode, user_group or GROUP_RULE,
                mixed_port=self.mixed_port, api_port=self.api_port)
            cfg_path = os.path.join(self.workdir, "config.yaml")
            with open(cfg_path, "w", encoding="utf-8") as fout:
                fout.write(final)
            self._log_tail = []
            try:
                self.proc = subprocess.Popen(
                    [exe, "-d", self.workdir, "-f", cfg_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   if os.name == "nt" else 0),
                )
            except Exception as exc:
                self.proc = None
                return False, "failed to launch mihomo: %s" % exc
            threading.Thread(target=self._pump_stderr, daemon=True).start()
            threading.Thread(target=self._watchdog, args=(self.proc,),
                             daemon=True).start()
            # beta1.0.63: loopback-only micro-kernel. The ONLY thing this
            # accelerator owns is one mixed port on 127.0.0.1 — the system
            # proxy (Clash Verge's), DNS, hosts, adapters and routes are
            # never touched, which is what makes the two clients coexist.
            self.log("[accel] micro-kernel mode: loopback-only — "
                     "no system proxy, no virtual adapter, no wireless "
                     "changes, no route changes, no DNS/hosts changes")
            try:
                ready, why = self._wait_ready()
                if not ready:
                    self.stop()
                    return False, why
                # beta1.0.62: the isolation proof — the adapter landscape
                # must be IDENTICAL after the kernel is up. If a TUN/virtual
                # adapter ever appeared here, this line says so loudly
                # instead of the user's network silently breaking.
                census_after = _adapter_census()
                if census_after:
                    self.log("[accel] %s" % census_after)
                    if census_before and census_after != census_before:
                        self.log("[accel] ⚠ 网卡数量在启动后发生变化——"
                                 "立即停止并检查（加速器不应创建/删除网卡）")
                # beta1.0.61: probe every node and auto-select a reachable
                # one. On campus networks the subscription's first node is
                # very often on a filtered port; without this the very first
                # store click would hang on a dead endpoint even though
                # other nodes in the same subscription work fine.
                try:
                    self.probe_nodes(autoselect=True)
                except Exception as exc:
                    self.log("[accel] node probe skipped: %s" % exc)
                self.user_group = user_group
                self.save_pref(subUrl=source, mode=mode,
                               userGroup=user_group, clashProxy=clash_proxy)
                return True, self.status_text()
            except Exception as exc:
                self.stop()
                return False, "加速器启动异常：%s" % exc

    def stop(self):
        with self._lock:
            proc, self.proc = self.proc, None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                except Exception:
                    pass
                self.log("[accel] store accelerator stopped")

    def shutdown(self):
        self.stop()

    # -- internals ------------------------------------------------------------

    def _wait_ready(self, timeout=15):
        """Poll the loopback API until the selector group has real nodes.

        Returns (ok, reason). A subscription that parses to nothing —
        mihomo then synthesises the COMPATIBLE placeholder — is reported as
        a failure, so the user sees WHY instead of a store page that dies
        with ERR_CONNECTION_REFUSED.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running():
                tail = " ".join(self._log_tail[-2:])[-160:]
                return False, ("mihomo 启动后退出：%s —— 配置可能有语法错误"
                               % (tail or "unknown"))
            if self._group_nodes():
                return True, ""
            time.sleep(0.5)
        tail = " ".join(self._log_tail[-3:])[-200:]
        return False, ("订阅未能解析出可用节点（mihomo 只生成了 COMPATIBLE "
                       "占位）——请确认这是 Clash/Mihomo 格式订阅%s"
                       % ("；日志尾：%s" % tail if tail else ""))

    def _watchdog(self, proc):
        """If the kernel dies unexpectedly, log it loudly.

        beta1.0.63: there is no system proxy to restore — nothing was ever
        written to the registry. The micro-kernel dying is now a purely
        local event: whatever the user pointed at 127.0.0.1:<port> simply
        stops answering; Clash Verge and the rest of the system are
        completely unaffected.
        """
        try:
            proc.wait()
        except Exception:
            return
        with self._lock:
            if self.proc is proc:
                self.log("[accel] mihomo 已退出——本机代理端口随之失效"
                         "（不影响系统代理与其他网络）")

    def _pump_stderr(self):
        proc = self.proc
        seen_hints = set()
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                with self._lock:
                    self._log_tail.append(line)
                    del self._log_tail[:-20]
                    # Translate the kernel's dial failures into one Chinese
                    # hint per distinct cause — a flood of identical timeouts
                    # must not turn into a flood of identical log lines.
                    hint = _classify_dial_error(line)
                    if hint and hint not in seen_hints:
                        seen_hints.add(hint)
                        self.log("[accel] %s" % hint)
        except Exception:
            pass

    # -- loopback endpoint (display only) -----------------------------------

    def local_proxy_url(self):
        """The one thing this accelerator owns: a loopback mixed port.

        Displayed in the accel page so the user can point a browser, Steam
        or their Clash Verge at it. beta1.0.63: it is NEVER written into
        the system proxy — the user's own proxy keeps full control.
        """
        if not self.is_running():
            return ""
        return "http://127.0.0.1:%d  |  socks5://127.0.0.1:%d" % (
            self.mixed_port, self.mixed_port)
