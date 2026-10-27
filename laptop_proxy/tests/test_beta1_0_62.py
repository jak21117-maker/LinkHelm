# -*- coding: utf-8 -*-
"""beta1.0.62 regression tests.

Scope of this build:
  1. the single-file limit is raised 10 GB -> 30 GB on BOTH ends — the PC
     protocol constant, the Android constant, and every piece of user-facing
     copy (dialog titles, header text, log lines);
  2. the PC file-transfer dialog picks the receive folder FIRST — when the
     dialog opens (persisted as settings.json "recvDir") — and "Receive" no
     longer asks on every click.
"""

import base64
import os
import socket
import sys
from pathlib import Path

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_transfer as ft
import main

USER, PASS = "u", "p"


def _recv_line(sock):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            return buf.decode("utf-8", "replace")
        buf += chunk
        if len(buf) > 4096:
            break
    return buf.decode("utf-8", "replace").strip()


def _send_line(sock, text):
    sock.sendall((text + "\n").encode("utf-8"))


@pytest.fixture()
def paired(tmp_path, monkeypatch):
    store = tmp_path / "ft_paired.json"
    monkeypatch.setattr(ft, "_pair_store_path", lambda: str(store))
    ft.add_paired_ip("127.0.0.1")
    yield
    ft.remove_paired_ip("127.0.0.1")


# ---------------------------------------------------------------------------
# 30 GB limit
# ---------------------------------------------------------------------------

def test_single_file_limit_is_30gb():
    assert ft.FT_MAX_BYTES == 30_000_000_000


def test_sender_refuses_oversize_before_dialling(tmp_path, paired, monkeypatch):
    """A >30 GB file must fail before a single byte — and before dialling."""
    f = tmp_path / "huge.iso"
    f.write_bytes(b"x")
    real_getsize = os.path.getsize
    monkeypatch.setattr(
        ft.os.path, "getsize",
        lambda p: (ft.FT_MAX_BYTES + 1) if str(p) == str(f) else real_getsize(p))
    monkeypatch.setattr(ft, "is_paired", lambda _ip: True)

    def _boom(*_a, **_k):
        raise AssertionError("an oversize file must be refused before dialling")

    monkeypatch.setattr(ft.socket, "create_connection", _boom)
    with pytest.raises(ft.FileTransferError) as err:
        ft.send_file("127.0.0.1", USER, PASS, str(f))
    assert "30 GB" in str(err.value)


def test_receiver_rejects_offer_above_30gb(tmp_path, paired):
    save = tmp_path / "out"
    recv = ft.ReceiverServer("127.0.0.1", USER, PASS, str(save))
    recv.start()
    try:
        s = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
        try:
            s.settimeout(10)
            _send_line(s, f"AUTH {USER} {PASS}")
            assert _recv_line(s) == "OK"
            name_b64 = base64.b64encode(b"huge.iso").decode()
            _send_line(s, f"OFFER {name_b64} {ft.FT_MAX_BYTES + 1} "
                          f"{'0' * 64}")
            assert _recv_line(s) == "REJECT size"
        finally:
            s.close()
    finally:
        recv.stop()


# ---------------------------------------------------------------------------
# receive folder first
# ---------------------------------------------------------------------------

def test_settings_roundtrip_recvdir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        main, "SETTINGS_PATH", str(tmp_path / "cfg" / "settings.json"))
    # default: no folder remembered — the dialog asks on open
    assert main.load_app_settings()["recvDir"] == ""
    settings = main.load_app_settings()
    settings["recvDir"] = str(tmp_path / "incoming")
    main.save_app_settings(settings)
    assert main.load_app_settings()["recvDir"] == str(tmp_path / "incoming")


def test_android_constant_matches_30gb():
    kt = (Path(main.__file__).resolve().parents[1] / "app" / "src" / "main"
          / "java" / "com" / "linkhelm" / "app" / "FileTransfer.kt")
    text = kt.read_text(encoding="utf-8")
    assert "MAX_FILE_BYTES = 30_000_000_000L" in text
    assert "10_000_000_000" not in text


def test_dialog_copy_says_30gb():
    """Every user-facing limit string must read 30 GB, never 10 GB."""
    src = Path(main.__file__).resolve().read_text(encoding="utf-8")
    assert "30 GB" in src
    assert "10 GB" not in src
