# -*- coding: utf-8 -*-
"""beta1.0.57: store accelerator (Mihomo subprocess) unit tests.

Focus: the app NEVER bundles or generates nodes — the config only references
a user-supplied provider — and the shipped kernel binary is hash-verified.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store_accel


def test_generated_config_has_no_bundled_proxies():
    text = store_accel.generate_config_text("https://example.com/sub")
    # only a proxy-PROVIDER (user-supplied), never an inline proxies: list
    assert "proxy-providers:" in text
    assert "proxies:" not in text
    assert "https://example.com/sub" in text


def test_generated_config_store_rules_and_direct_fallback():
    text = store_accel.generate_config_text("https://example.com/sub")
    assert "mixed-port: %d" % store_accel.MIXED_PORT in text
    assert "DOMAIN-SUFFIX,steampowered.com,STORE" in text
    assert "DOMAIN-SUFFIX,epicgames.com,STORE" in text
    assert "allow-lan: false" in text
    assert "bind-address: 127.0.0.1" in text
    # everything non-store stays DIRECT (no geo database download needed)
    assert "MATCH,DIRECT" in text


def test_bundled_kernel_binary_hash_verifies():
    # the repo ships the official v1.19.33 exe next to engine/; if present
    # its SHA-256 must equal the pinned constant exactly.
    repo_exe = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "mihomo", "mihomo.exe")
    if os.path.isfile(repo_exe):
        path, err = store_accel.find_mihomo_exe()
        assert err is None
        assert path is not None and os.path.isfile(path)


def test_start_rejects_empty_subscription(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    ok, msg = acc.start("")
    assert ok is False
    # the message must make the no-bundled-nodes policy explicit
    assert "no nodes are bundled" in msg


def test_start_rejects_non_url_non_file(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    ok, msg = acc.start("not-a-url-or-file")
    assert ok is False


def test_status_stopped_by_default(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    assert acc.is_running() is False
    assert acc.status_text() == ""


# --- beta1.0.58: routing mode + loopback node API --------------------------

def test_generated_config_rule_mode_is_default():
    text = store_accel.generate_config_text("https://example.com/sub")
    assert "mode: rule" in text
    assert "mode: global" not in text


def test_generated_config_global_mode():
    text = store_accel.generate_config_text("https://example.com/sub",
                                            mode="global")
    assert "mode: global" in text
    # global mode keeps the provider — nodes still come from the USER
    assert "proxy-providers:" in text
    assert "proxies:" not in text


def test_generated_config_exposes_loopback_api_only():
    text = store_accel.generate_config_text("https://example.com/sub")
    assert "external-controller: 127.0.0.1:%d" % store_accel.API_PORT in text
    assert store_accel.API_BASE.startswith("http://127.0.0.1:")


def test_group_name_follows_mode(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    acc.mode = "rule"
    assert acc._group_name() == store_accel.GROUP_RULE
    acc.mode = "global"
    assert acc._group_name() == store_accel.GROUP_GLOBAL


def test_start_coerces_unknown_mode_to_rule(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    ok, _ = acc.start("", mode="bogus")
    assert ok is False
    assert acc.mode == "rule"


def test_node_api_is_inert_while_stopped(tmp_path):
    acc = store_accel.StoreAccel(str(tmp_path), lambda msg: None)
    assert acc.list_nodes() == []
    assert acc.current_node() == ""
    ok, msg = acc.select_node("HK")
    assert ok is False
    assert "not running" in msg
