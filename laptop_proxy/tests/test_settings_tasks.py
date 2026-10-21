# -*- coding: utf-8 -*-
"""v1.5.44 — user-gated autostart & persisted settings.

Autostart is now USER-controlled (never unconditional): a trigger-less
ElevatedLauncher task keeps silent elevation, and LoginStartup exists ONLY
while the user enabled it in Settings. settings.json stores connectOnLaunch.
"""
import json
import os

import pytest

import main


@pytest.fixture()
def fake_settings_path(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        main, "SETTINGS_PATH", str(tmp_path / "cfg" / "settings.json")
    )
    return tmp_path / "cfg" / "settings.json"


# ── load_app_settings ────────────────────────────────────────────────────
def test_load_defaults_when_missing(fake_settings_path):
    assert not fake_settings_path.exists()
    s = main.load_app_settings()
    assert s["startAtLogon"] is False
    assert s["connectOnLaunch"] is False
    assert s["schemaVersion"] == 1


def test_load_roundtrip(fake_settings_path):
    main.save_app_settings({"schemaVersion": 1, "startAtLogon": True,
                            "connectOnLaunch": True})
    s = main.load_app_settings()
    assert s["startAtLogon"] is True
    assert s["connectOnLaunch"] is True


def test_load_corrupt_file_gives_defaults(fake_settings_path):
    fake_settings_path.parent.mkdir(parents=True)
    fake_settings_path.write_text("{not json", encoding="utf-8")
    s = main.load_app_settings()
    assert s["startAtLogon"] is False


# ── save_app_settings ────────────────────────────────────────────────────
def test_save_atomic_no_tmp_leftover(fake_settings_path):
    main.save_app_settings({"schemaVersion": 1, "startAtLogon": False,
                            "connectOnLaunch": False})
    assert fake_settings_path.exists()
    assert not os.path.exists(str(fake_settings_path) + ".tmp")


# ── _sync_login_startup (Task Scheduler is the source of truth) ─────────
def test_sync_login_enable_runs_schtasks_create(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(main, "_run_decoded", fake_run)
    monkeypatch.setattr(main, "_schtasks_task_exists", lambda name: True)
    assert main._sync_login_startup(True) is True
    create = [c for c in calls if "/create" in c]
    assert create, "expected schtasks /create"
    # /tr value = "<exe>" --login-startup  (embedded in ONE list element)
    tr_value = create[0][create[0].index("/tr") + 1]
    assert main.LOGIN_STARTUP_ARG in tr_value
    assert main.SCHTASKS_LOGIN_NAME in create[0]


def test_sync_login_disable_runs_schtasks_delete(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(main, "_run_decoded", fake_run)
    monkeypatch.setattr(main, "_schtasks_task_exists", lambda name: False)
    # login task absent → nothing to delete
    assert main._sync_login_startup(False) is True


def test_sync_login_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        main, "_run_decoded", lambda cmd, timeout=None: (1, "", "boom")
    )
    monkeypatch.setattr(main, "_schtasks_task_exists", lambda name: False)
    assert main._sync_login_startup(True) is False


# ── _ensure_elevation_task (trigger-less, Register-ScheduledTask) ───────
def test_ensure_elevation_task_uses_register_without_trigger(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=None):
        seen["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(main, "_run_decoded", fake_run)
    monkeypatch.setattr(
        main, "_schtasks_task_exists",
        lambda name: name == main.SCHTASKS_LAUNCH_NAME
    )
    assert main._ensure_elevation_task() is True
    ps = seen["cmd"][-1]
    assert "Register-ScheduledTask" in ps
    assert "ElevatedLauncher" in ps
    assert "-Trigger" not in ps, "elevation task must have NO trigger"
    assert "RunLevel Highest" in ps


def test_ensure_elevation_task_failure_false(monkeypatch):
    monkeypatch.setattr(
        main, "_run_decoded", lambda cmd, timeout=None: (1, "", "denied")
    )
    monkeypatch.setattr(main, "_schtasks_task_exists", lambda name: False)
    assert main._ensure_elevation_task() is False


