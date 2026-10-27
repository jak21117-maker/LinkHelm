# -*- coding: utf-8 -*-
"""beta1.0.59: Clash subscription compatibility.

Panels hand out subscriptions in several shapes; all must work:
  1. full Clash/Mihomo YAML (proxies + proxy-groups + rules)
  2. base64-encoded Clash YAML
  3. base64/plain lists of v2ray URIs (ss/vmess/trojan/vless/hysteria2)
  4. local .yaml files
The kernel itself only accepts Clash config, so shapes 2-4 are normalised
locally before launch.
"""
import base64
import os
import subprocess

import store_accel


FULL_CLASH = """\
port: 7890
socks-port: 7891
mode: rule
log-level: info
external-controller: 127.0.0.1:9090
proxies:
  - name: "Node-A"
    type: ss
    server: a.example.com
    port: 8388
    cipher: aes-256-gcm
    password: pass1
  - name: "Node-B"
    type: trojan
    server: b.example.com
    port: 443
    password: pass2
proxy-groups:
  - name: "Manual Switch"
    type: select
    proxies:
      - Node-A
      - Node-B
rules:
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - MATCH,DIRECT
"""


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _sample_uris():
    ss = "ss://" + base64.b64encode(b"aes-256-gcm:password").decode() + \
         "@ss.example.com:8388#SS Node"
    vmess = "vmess://" + _b64(
        '{"v":"2","ps":"VMess Node","add":"vmess.example.com","port":"443",'
        '"id":"b831381d-6324-4d53-ad4f-8cda48b30886","aid":"0",'
        '"scy":"auto","net":"ws","tls":"tls","host":"ws.example.com",'
        '"path":"/ray"}')
    trojan = "trojan://password@trojan.example.com:443?sni=trojan.example.com#Trojan Node"
    vless = ("vless://b831381d-6324-4d53-ad4f-8cda48b30886@vless.example.com:443"
             "?encryption=none&security=tls&type=ws&host=ws.example.com"
             "&path=%2Fvless#VLESS Node")
    hy2 = "hysteria2://authpw@hy2.example.com:443?sni=hy2.example.com#HY2 Node"
    return "\n".join([ss, vmess, trojan, vless, hy2])


# ---------------------------------------------------------------------------
# shape 1: full Clash YAML
# ---------------------------------------------------------------------------

def test_full_clash_yaml_keeps_user_group_and_injects_store_rules():
    group = store_accel._first_select_group(FULL_CLASH)
    assert group == "Manual Switch"
    cfg = store_accel._build_config_from_user_yaml(FULL_CLASH, "rule", group)
    # our managed keys appear exactly once (duplicates would break mihomo).
    # "\nmode:" (line-anchored) so the injected DNS block's
    # "  enhanced-mode: fake-ip" is not miscounted.
    for key in ("\nmixed-port:", "\nexternal-controller:", "\nmode:",
                "\nlog-level:"):
        assert cfg.count(key) == 1, key
    # the user's own ports must not survive as duplicates
    assert "\nport: 7890\n" not in cfg
    assert "\nsocks-port: 7891\n" not in cfg
    # the user's selector group is preserved and reachable via the API port
    assert 'name: "Manual Switch"' in cfg
    # beta1.0.61 GRAFT: in rule mode the ONLY rules are ours — the
    # subscription's own rules (incl. a MATCH,<node> tail that would push the
    # whole PC through the node) are dropped, everything non-store is DIRECT.
    rules = cfg[cfg.index("\nrules:"):]
    assert "DOMAIN-SUFFIX,steampowered.com,Manual Switch" in rules
    assert "DOMAIN-SUFFIX,epicgames.com,Manual Switch" in rules
    assert "IP-CIDR,192.168.0.0/16,DIRECT" not in rules
    assert rules.rstrip().endswith("MATCH,DIRECT")


def test_full_clash_yaml_global_mode_drops_rules():
    group = store_accel._first_select_group(FULL_CLASH)
    cfg = store_accel._build_config_from_user_yaml(FULL_CLASH, "global", group)
    assert "mode: global" in cfg
    # global mode is the explicit "everything via the node" choice — mihomo
    # ignores rules there, so they are removed entirely.
    assert "\nrules:" not in cfg
    assert 'name: "Manual Switch"' in cfg


# ---------------------------------------------------------------------------
# shape 2: base64-encoded Clash YAML
# ---------------------------------------------------------------------------

def test_base64_clash_yaml_is_recognised():
    body = _b64(FULL_CLASH)
    cfg = store_accel._normalize_subscription_body(body)
    assert "proxies:" in cfg
    assert "Node-A" in cfg


# ---------------------------------------------------------------------------
# shape 3: base64 / plain v2ray URI lists (the …/get?token=… panels)
# ---------------------------------------------------------------------------

def test_base64_uri_list_is_converted_to_clash():
    body = _b64(_sample_uris())
    cfg = store_accel._normalize_subscription_body(body)
    assert "proxies:" in cfg
    assert 'type: "ss"' in cfg
    assert 'type: "vmess"' in cfg
    assert 'type: "trojan"' in cfg
    assert 'type: "vless"' in cfg
    assert 'type: "hysteria2"' in cfg
    # every node carries a unique name
    names = [l for l in cfg.splitlines() if l.strip().startswith("- name:")]
    assert len(names) == 5
    assert len(set(names)) == 5


def test_plain_uri_list_pasted_directly():
    cfg = store_accel._normalize_subscription_body(_sample_uris())
    assert 'type: "vmess"' in cfg


def test_uri_conversion_preserves_transport_options():
    cfg = store_accel._uris_to_config_text(
        "vmess://" + _b64(
            '{"v":"2","ps":"WS","add":"h.example.com","port":"80",'
            '"id":"b831381d-6324-4d53-ad4f-8cda48b30886","aid":"0",'
            '"net":"ws","path":"/dl","host":"front.example.com"}'))
    assert 'network: "ws"' in cfg
    assert 'path: "/dl"' in cfg
    assert 'Host: "front.example.com"' in cfg
    assert 'tls: true' not in cfg        # tls absent in the URI


def test_uri_conversion_skips_unsupported_schemes():
    cfg = store_accel._uris_to_config_text("ssr://abcdef\nnot a uri\n")
    assert cfg == ""


def test_garbage_body_is_rejected():
    assert store_accel._normalize_subscription_body("<html>404</html>") == ""


# ---------------------------------------------------------------------------
# end-to-end: the kernel must ACCEPT what we generate
# ---------------------------------------------------------------------------

def _mihomo_exe():
    path = os.path.join(os.path.dirname(os.path.abspath(store_accel.__file__)),
                        "mihomo", "mihomo.exe")
    return path if os.path.isfile(path) else None


def test_mihomo_accepts_uri_converted_config(tmp_path):
    exe = _mihomo_exe()
    if exe is None or os.name != "nt":
        return  # no binary on this box — validated on Windows CI instead
    cfg_text = store_accel._normalize_subscription_body(_b64(_sample_uris()))
    cfg = store_accel._build_config_from_user_yaml(cfg_text, "rule", "")
    workdir = str(tmp_path / "mihomo")
    os.makedirs(workdir)
    cfg_path = os.path.join(workdir, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fout:
        fout.write(cfg)
    rc = subprocess.run([exe, "-d", workdir, "-f", cfg_path, "-t"],
                        capture_output=True, timeout=60,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert rc.returncode == 0, (rc.stdout + rc.stderr).decode("utf-8", "replace")


def test_mihomo_accepts_full_clash_config(tmp_path):
    exe = _mihomo_exe()
    if exe is None or os.name != "nt":
        return
    cfg = store_accel._build_config_from_user_yaml(
        FULL_CLASH, "rule", store_accel._first_select_group(FULL_CLASH))
    workdir = str(tmp_path / "mihomo")
    os.makedirs(workdir)
    cfg_path = os.path.join(workdir, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fout:
        fout.write(cfg)
    rc = subprocess.run([exe, "-d", workdir, "-f", cfg_path, "-t"],
                        capture_output=True, timeout=60,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert rc.returncode == 0, (rc.stdout + rc.stderr).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# placeholder handling (the old "COMPATIBLE only" failure)
# ---------------------------------------------------------------------------

def test_compatible_placeholder_is_not_a_node():
    assert "COMPATIBLE" in store_accel.PLACEHOLDER_PROXIES


def test_start_rejects_unknown_input(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    ok, msg = acc.start("E0NzE6Mzl4YmExOTg4Njg1", mode="rule")
    assert ok is False
    assert "无法识别" in msg
