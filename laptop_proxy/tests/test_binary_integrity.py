# -*- coding: utf-8 -*-
"""beta1.0.5 — runtime integrity lock for the bundled lwIP engine.

The program runs elevated, so a swapped hev-socks5-tunnel.exe / wintun.dll /
msys-2.0.dll under BIN_DIR/engine/ must never execute. _check_dependencies
must (a) trust a verified engine/ trio, (b) refuse a tampered or missing
component with a loud RuntimeError. No runtime download exists anymore —
tun2socks (and its bundled_bin self-heal) was removed entirely; the engine
ships with the package and the SHA-256 lock pins the official 2.19.1 build.
"""
import os
import sys
import types

import pytest

import engine
import main


@pytest.fixture()
def fake_engine_dir(tmp_path, monkeypatch):
    """Isolated engine.BIN_DIR with an engine/ subdir holding valid placeholder files.

    EXPECTED_BIN_SHA256 is re-pinned to the placeholder hashes for the duration
    of the test (the official hashes cannot be reproduced from placeholder
    bytes) and restored afterwards.
    """
    bin_dir = tmp_path / "bin"
    engine_dir = bin_dir / "engine"
    engine_dir.mkdir(parents=True)
    names = (engine.LWIP_EXE_NAME, engine.WINTUN_DLL_NAME, engine.MSYS_DLL_NAME)
    saved = dict(main.EXPECTED_BIN_SHA256)
    good = {name: (b"OFFICIAL-" + name.encode()) for name in names}
    for name, data in good.items():
        (engine_dir / name).write_bytes(data)
        main.EXPECTED_BIN_SHA256[name] = main._file_sha256(str(engine_dir / name))
    monkeypatch.setattr(engine, "BIN_DIR", str(bin_dir))
    try:
        yield types.SimpleNamespace(bin_dir=bin_dir, engine_dir=engine_dir, good=good)
    finally:
        main.EXPECTED_BIN_SHA256.clear()
        main.EXPECTED_BIN_SHA256.update(saved)


def _make_app(logs):
    app = object.__new__(main.TunManager)
    app.log = logs.append
    return app


def test_valid_engine_binaries_are_trusted(fake_engine_dir):
    logs = []
    app = _make_app(logs)
    result = app._check_dependencies()
    assert result == os.path.join(
        str(fake_engine_dir.engine_dir), "hev-socks5-tunnel.exe"
    )
    assert any("lwIP engine verified" in line for line in logs)


def test_tampered_engine_binary_is_rejected(fake_engine_dir):
    (fake_engine_dir.engine_dir / "wintun.dll").write_bytes(b"TAMPERED-PAYLOAD")
    logs = []
    app = _make_app(logs)
    with pytest.raises(RuntimeError, match="fails SHA-256"):
        app._check_dependencies()
    assert any("lwIP engine component" in line for line in logs)


def test_missing_engine_binary_is_rejected(fake_engine_dir):
    (fake_engine_dir.engine_dir / "msys-2.0.dll").unlink()
    app = _make_app([])
    with pytest.raises(RuntimeError, match="missing or fails SHA-256"):
        app._check_dependencies()


def test_engine_dir_is_the_only_source(fake_engine_dir):
    """beta1.0.5: no runtime download / no bundled_bin self-heal — the engine
    trio must live in BIN_DIR/engine/ and nowhere else."""
    app = _make_app([])
    exe = app._check_dependencies()
    assert exe == os.path.join(str(fake_engine_dir.bin_dir), "engine",
                               "hev-socks5-tunnel.exe")


def test_integrity_lock_matches_shipped_engine():
    """The shipped engine/ binaries must hash-match the lock exactly (this is
    what the elevated runtime re-verification relies on). Skipped when the
    engine/ directory is not present (e.g. a source-only checkout)."""
    engine_dir = engine.engine_bin_dir()
    if not os.path.isdir(engine_dir):
        pytest.skip("engine/ not shipped in this checkout")
    for name in (engine.LWIP_EXE_NAME, engine.WINTUN_DLL_NAME, engine.MSYS_DLL_NAME):
        path = os.path.join(engine_dir, name)
        assert os.path.isfile(path), f"{name} missing from engine/"
        assert main._file_sha256(path) == main.EXPECTED_BIN_SHA256[name], (
            f"{name} does not match the integrity lock"
        )


def test_frozen_mode_anchors_bin_dir_to_meipass(tmp_path, monkeypatch):
    """beta1.0.5 regression: in the frozen onedir package the engine trio
    lives at <app>/_internal/engine/ (LaptopProxy.spec binaries target
    'engine'). engine.py itself is PYZ-embedded — NOT a file on disk — so
    __file__ must NOT be the anchor. sys._MEIPASS is the canonical
    PyInstaller root and equals <app>/_internal in onedir mode."""
    with open(engine.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    meipass = tmp_path / "_internal"
    (meipass / "engine").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    probe = types.ModuleType("engine_frozen_probe")
    exec(compile(src, "engine_frozen_probe", "exec"), probe.__dict__)
    assert probe.BIN_DIR == str(meipass)
    assert probe.engine_bin_dir() == os.path.join(str(meipass), "engine")
    assert probe.engine_exe_path() == os.path.join(
        str(meipass), "engine", "hev-socks5-tunnel.exe"
    )
