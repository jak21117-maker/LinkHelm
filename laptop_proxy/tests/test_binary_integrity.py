# -*- coding: utf-8 -*-
"""v1.4.4 — runtime integrity lock for bundled/installed VPN binaries.

The program runs elevated, so a swapped tun2socks.exe / wintun.dll in BIN_DIR
must never execute. _check_dependencies must (a) refuse a tampered installed
copy, (b) self-heal from a verified bundled copy, (c) fail loudly when the
bundled copies no longer match the integrity lock.
"""
import os
import sys
import types

import pytest

import main


@pytest.fixture(autouse=True)
def fake_version_ok(monkeypatch):
    """v1.5.4: placeholder bytes can't report a real `--version`; the P0-1
    runtime version gate is monkeypatched per-test (these tests exercise the
    HASH path). test_version_gate_* re-patches it to test the version path.
    """
    monkeypatch.setattr(main, "_tun2socks_version_ok", lambda path: True)


@pytest.fixture()
def fake_dirs(tmp_path, monkeypatch):
    """Isolated BIN_DIR + bundled_bin with valid placeholder content."""
    bin_dir = tmp_path / "bin"
    bundled = tmp_path / "bundled_bin"
    bin_dir.mkdir()
    bundled.mkdir()
    good = {name: (b"OFFICIAL-" + name.encode()) for name in main.EXPECTED_BIN_SHA256}
    for name, data in good.items():
        (bundled / name).write_bytes(data)
        main.EXPECTED_BIN_SHA256[name] = main._file_sha256(str(bundled / name))
    monkeypatch.setattr(main, "BIN_DIR", str(bin_dir))
    monkeypatch.setattr(main, "_bundled_bin_dir", lambda: str(bundled))
    return types.SimpleNamespace(bin_dir=bin_dir, bundled=bundled, good=good)


def _make_app(logs):
    app = object.__new__(main.TunManager)
    app.log = logs.append
    app.confirm_download = lambda: False
    return app


def test_valid_installed_binaries_are_trusted(fake_dirs):
    for name, data in fake_dirs.good.items():
        (fake_dirs.bin_dir / name).write_bytes(data)
    logs = []
    app = _make_app(logs)
    result = app._check_dependencies()
    assert result == os.path.join(str(fake_dirs.bin_dir), "tun2socks.exe")
    assert not any("安全警告" in line for line in logs)


def test_tampered_installed_binary_is_removed_and_healed_from_bundled(fake_dirs):
    t2s = fake_dirs.bin_dir / "tun2socks.exe"
    t2s.write_bytes(b"TAMPERED-PAYLOAD")
    logs = []
    app = _make_app(logs)
    result = app._check_dependencies()
    assert result == str(t2s)
    assert main._file_sha256(str(t2s)) == main.EXPECTED_BIN_SHA256["tun2socks.exe"]
    assert any("Security warning" in line for line in logs)
    # healed copy equals the bundled (official) bytes
    assert t2s.read_bytes() == (fake_dirs.bundled / "tun2socks.exe").read_bytes()


def test_tampered_bundled_copy_is_rejected(fake_dirs):
    (fake_dirs.bundled / "wintun.dll").write_bytes(b"EVIL")
    logs = []
    app = _make_app(logs)
    with pytest.raises(RuntimeError):
        app._check_dependencies()
    assert any("failed hash verification" in line for line in logs)
    assert not (fake_dirs.bin_dir / "wintun.dll").exists()


def test_v154_legacy_versioned_backups_are_swept_from_cache(fake_dirs):
    """v1.5.4: the runtime cache must never keep per-version backups such as
    tun2socks_v2.5.2_backup.exe — only the two pinned files may live there."""
    (fake_dirs.bin_dir / "tun2socks_v2.5.2_backup.exe").write_bytes(b"OLD")
    (fake_dirs.bin_dir / "tun2socks_v2.7.0_official.exe").write_bytes(b"REF")
    logs = []
    app = _make_app(logs)
    app._check_dependencies()
    assert not (fake_dirs.bin_dir / "tun2socks_v2.5.2_backup.exe").exists()
    assert not (fake_dirs.bin_dir / "tun2socks_v2.7.0_official.exe").exists()
    assert any("Cleaned legacy cache component" in line for line in logs)


def test_v154_version_gate_reinstalls_stale_cached_build(fake_dirs, monkeypatch):
    """v1.5.4 (P0-1): a cached binary that passes the HASH gate but reports a
    foreign build (e.g. 2.5.2) is removed and reinstalled from the bundled
    v2.7.0 build — the only recovery source."""
    for name, data in fake_dirs.good.items():
        (fake_dirs.bin_dir / name).write_bytes(data)
    calls = []

    def fake_version(path):
        calls.append(path)
        # first call inspects the CACHED copy → foreign build; every later
        # call inspects the freshly installed bundled copy → pinned build.
        return len(calls) > 1

    monkeypatch.setattr(main, "_tun2socks_version_ok", fake_version)
    logs = []
    app = _make_app(logs)
    result = app._check_dependencies()
    assert result == os.path.join(str(fake_dirs.bin_dir), "tun2socks.exe")
    assert any("version check failed" in line for line in logs)
    assert any("version verified" in line for line in logs)
