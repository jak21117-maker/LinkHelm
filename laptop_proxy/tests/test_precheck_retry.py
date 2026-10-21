"""v1.4.2 stability tests: precheck retry logic (check_socks5_with_retry).

Wi-Fi Direct power-save swallows single connect attempts (field: tun2socks
"i/o timeout" blips to 192.168.49.1:8080). The precheck must tolerate transient
timeouts with retries and fail fast on permanent protocol errors.
"""
import importlib
from pathlib import Path
import sys

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


def test_retry_recovers_after_transient_timeouts(monkeypatch):
    calls = {"n": 0}

    def fake_check(host, port, timeout=3.0, **kwargs):
        calls["n"] += 1
        return "TCP_CONNECT_TIMEOUT" if calls["n"] < 3 else "SOCKS5_OK"

    monkeypatch.setattr(main, "check_socks5", fake_check)
    sleeps = []
    result = main.check_socks5_with_retry(
        "192.168.49.1", 8080, attempts=3, delay=2.0,
        log_fn=None, sleep_fn=lambda d: sleeps.append(d)
    )
    assert result == "SOCKS5_OK"
    assert calls["n"] == 3
    assert sleeps == [2.0, 2.0]


def test_retry_exhausted_returns_last_transient_result(monkeypatch):
    calls = {"n": 0}

    def fake_check(host, port, timeout=3.0, **kwargs):
        calls["n"] += 1
        return "TCP_CONNECT_TIMEOUT"

    monkeypatch.setattr(main, "check_socks5", fake_check)
    result = main.check_socks5_with_retry(
        "192.168.49.1", 8080, attempts=3, delay=0.0, sleep_fn=lambda _d: None
    )
    assert result == "TCP_CONNECT_TIMEOUT"
    assert calls["n"] == 3


def test_permanent_error_fails_fast_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_check(host, port, timeout=3.0, **kwargs):
        calls["n"] += 1
        return "SOCKS5_NO_ACCEPTABLE_METHOD"

    monkeypatch.setattr(main, "check_socks5", fake_check)
    result = main.check_socks5_with_retry(
        "192.168.49.1", 8080, attempts=3, delay=0.0, sleep_fn=lambda _d: None
    )
    assert result == "SOCKS5_NO_ACCEPTABLE_METHOD"
    assert calls["n"] == 1


def test_ok_first_try_makes_no_extra_calls(monkeypatch):
    calls = {"n": 0}

    def fake_check(host, port, timeout=3.0, **kwargs):
        calls["n"] += 1
        return "SOCKS5_OK"

    monkeypatch.setattr(main, "check_socks5", fake_check)
    result = main.check_socks5_with_retry(
        "192.168.49.1", 8080, attempts=3, delay=0.0, sleep_fn=lambda _d: None
    )
    assert result == "SOCKS5_OK"
    assert calls["n"] == 1


def test_transient_classifier():
    assert main.is_transient_precheck("TCP_CONNECT_TIMEOUT")
    assert main.is_transient_precheck("SOCKS5_HANDSHAKE_TIMEOUT")
    assert main.is_transient_precheck("TCP_CONNECT_FAILED(ConnectionRefusedError)")
    assert main.is_transient_precheck("SOCKS5_IO_FAILED(ConnectionResetError)")
    assert not main.is_transient_precheck("SOCKS5_NO_ACCEPTABLE_METHOD")
    assert not main.is_transient_precheck("SOCKS5_BAD_REPLY")
    assert not main.is_transient_precheck("INVALID_TARGET")
    assert not main.is_transient_precheck("SOCKS5_OK")
