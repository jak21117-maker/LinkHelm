"""beta1.0.59: resumable file-transfer regression tests.

Drives the real ReceiverServer over loopback:

  1. a first attempt sends ~9 MB of a 12 MB file and then dies mid-stream
     (peer exit simulation) — the receiver must keep the .part AND a fresh
     .meta checkpoint (8 MB), and must keep listening;
  2. a second attempt of the SAME file must be answered "RESUME 8388608"
     and finish with a byte-identical, SHA-256-verified file;
  3. stop() must wipe the staging pair so a fresh receiver never resumes.
  (beta1.0.59: the staged pair and the published file carry the
   SENDER's file name; the picked path only supplies the directory.)
"""
import hashlib
import importlib
import os
import socket
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("customtkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ft = importlib.import_module("file_transfer")

USER, PASS = "u", "p"
CHECKPOINT = 8 * 1024 * 1024


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


def _wait_part(path, minimum, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) >= minimum:
            return os.path.getsize(path)
        time.sleep(0.05)
    return os.path.getsize(path) if os.path.exists(path) else -1


def test_mid_file_drop_keeps_checkpoint_and_resumes(tmp_path, paired):
    payload = os.urandom(12 * 1024 * 1024 + 12345)  # > one 8 MB checkpoint
    src = tmp_path / "src_dir" / "src.bin"
    src.parent.mkdir()
    src.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    save = tmp_path / "out.bin"          # the picked path (directory only)
    target = tmp_path / "src.bin"        # beta1.0.59: real name from OFFER
    part = str(target) + ".part"
    meta = part + ".meta"

    recv = ft.ReceiverServer("127.0.0.1", USER, PASS, str(save))
    recv.start()
    try:
        # --- attempt 1: send 9 MB, then die mid-stream (peer exit) ---
        s = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
        try:
            s.settimeout(10)
            _send_line(s, f"AUTH {USER} {PASS}")
            assert _recv_line(s) == "OK"
            import base64
            name_b64 = base64.b64encode(b"src.bin").decode()
            _send_line(s, f"OFFER {name_b64} {len(payload)} {sha}")
            assert _recv_line(s) == "ACCEPT"
            s.sendall(payload[:9 * 1024 * 1024])
            time.sleep(0.3)  # let the receiver drain into the .part
        finally:
            s.close()  # abrupt drop, mid-file

        # the receiver must keep the staging pair; the .part holds every byte
        # that was flushed on the drop, the .meta records the 8 MB checkpoint
        size = _wait_part(part, CHECKPOINT)
        assert size >= CHECKPOINT, f"expected a staged partial, got {size}"
        assert os.path.exists(meta), "the .meta sidecar must survive the drop"
        import json
        with open(meta, encoding="utf-8") as fin:
            sidecar = json.load(fin)
        # the checkpoint is the first received offset at/after 8 MB (chunk
        # boundaries are not exact), and it must sit strictly inside the file
        assert CHECKPOINT <= sidecar["part_size"] < len(payload)
        assert sidecar["sha"] == sha
        checkpoint = sidecar["part_size"]

        # --- attempt 2: a fresh sender must be told where to continue ---
        probe = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
        try:
            probe.settimeout(10)
            _send_line(probe, f"AUTH {USER} {PASS}")
            assert _recv_line(probe) == "OK"
            _send_line(probe, f"OFFER {name_b64} {len(payload)} {sha}")
            assert _recv_line(probe) == f"RESUME {checkpoint}"
        finally:
            probe.close()
        time.sleep(0.3)  # let the refused probe unwind before the real send

        # --- attempt 3: the real resume through the public sender API ---
        ft.send_file("127.0.0.1", USER, PASS, str(src))
        assert target.exists(), "the resumed transfer must publish the file"
        assert target.read_bytes() == payload
        assert not os.path.exists(part)
        assert not os.path.exists(meta)
    finally:
        recv.stop()
        # stop() wipes staging even if the test body failed mid-way
        assert not os.path.exists(part)
        assert not os.path.exists(meta)


def test_stop_wipes_staging_after_drop(tmp_path, paired):
    payload = os.urandom(12 * 1024 * 1024)  # only part of it will be sent
    src = tmp_path / "src_dir" / "src.bin"
    src.parent.mkdir()
    src.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    save = tmp_path / "out.bin"
    part = str(tmp_path / "src.bin") + ".part"

    recv = ft.ReceiverServer("127.0.0.1", USER, PASS, str(save))
    recv.start()
    try:
        s = socket.create_connection(("127.0.0.1", ft.FT_PORT), timeout=5)
        try:
            s.settimeout(10)
            _send_line(s, f"AUTH {USER} {PASS}")
            assert _recv_line(s) == "OK"
            import base64
            name_b64 = base64.b64encode(b"src.bin").decode()
            _send_line(s, f"OFFER {name_b64} {len(payload)} {sha}")
            assert _recv_line(s) == "ACCEPT"
            s.sendall(payload[:9 * 1024 * 1024])  # then die mid-file
        finally:
            s.close()
        assert _wait_part(part, CHECKPOINT) >= CHECKPOINT
    finally:
        recv.stop()
    assert not os.path.exists(part)
    assert not os.path.exists(part + ".meta")
