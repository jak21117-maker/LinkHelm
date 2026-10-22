import customtkinter as ctk
from tkinter import messagebox
from tkinter import filedialog
import file_transfer
import queue
import random
import re
import threading
import socket
import struct
import select
import sys
import os
import time
import http.client
import subprocess
import urllib.parse
import urllib.request
import zipfile
import io
import hashlib
import platform
import webbrowser
from console_bridge import BridgeError, IcsManager
# import symbols

# --- Windows specific imports ---
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import winreg
    import ctypes

# v1.4.93: keep the system awake while the tunnel runs. Long sessions were
# dying after Windows entered Modern Standby / idle sleep (the user reports
# "a process mounted too long gets killed by the Windows power policy" — the OS suspends the process tree
# and the Wi-Fi Direct link, tun2socks freezes, the tunnel looks dead).
# ES_SYSTEM_REQUIRED holds the *system* awake (display/free CPU policy is NOT
# touched, so this stays light); ES_CONTINUOUS keeps the effect until we
# clear it. Effect is per-thread → refreshed by a dedicated tunnel-scoped
# thread and always cleared in its finally block.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

# v1.5.42 (P0): SILENT subprocess layer. The "popup storm" on start/stop were
# console windows: every netsh / route / pnputil / powershell / taskkill call
# spawned a visible conhost window unless it explicitly passed
# CREATE_NO_WINDOW — and the GUI is a windowed app, so each of the ~14 call
# sites flashed a black window during connect, rebuild and teardown. One
# central patch: default EVERY subprocess.run/Popen in this process to
# CREATE_NO_WINDOW (callers may still override explicitly).
if IS_WINDOWS:
    _CREATE_NO_WINDOW = 0x08000000
    _orig_subprocess_run = subprocess.run
    _orig_subprocess_popen = subprocess.Popen

    def _silent_run(*args, **kwargs):
        kwargs.setdefault("creationflags", _CREATE_NO_WINDOW)
        return _orig_subprocess_run(*args, **kwargs)

    def _silent_popen(*args, **kwargs):
        kwargs.setdefault("creationflags", _CREATE_NO_WINDOW)
        return _orig_subprocess_popen(*args, **kwargs)

    subprocess.run = _silent_run
    subprocess.Popen = _silent_popen


# --- Runtime UI language (i18n) --------------------------------------------
# The external UI defaults to English. The user can switch to Chinese from
# Settings; the app relaunches so every surface (main window + tray) updates
# immediately. Internal comments and logs stay in English.
LANG = "en"  # "en" (default) | "zh"


def tr(en_text, zh_text):
    """Return the UI string for the active language.

    en_text is shown when LANG == "en" (default); zh_text when LANG == "zh".
    """
    return zh_text if LANG == "zh" else en_text


def _set_system_awake(keep_awake: bool) -> bool:
    """v1.4.93: best-effort SetThreadExecutionState wrapper. True on success."""
    if not IS_WINDOWS:
        return False
    try:
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if keep_awake else 0)
        prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return bool(prev)  # 0 = failed
    except Exception:
        return False


# --- v1.5.4 (P0): Windows suspend/resume notification ---------------------------
# The callback runs on a Win32 system thread and ONLY sets an Event — every
# recovery decision stays inside the single TunManager state machine, never
# in the callback (no Tk access, no netsh, no rebuild here).
_PBT_APMRESUMEAUTOMATIC = 0x12
_PBT_APMRESUMESUSPEND = 0x13
_DEVICE_NOTIFY_CALLBACK = 2


def _register_resume_notification(set_event):
    """PowerRegisterSuspendResumeNotification(DEVICE_NOTIFY_CALLBACK).

    Returns an opaque registration object (truthy) or None on any failure —
    resume detection is best-effort; the periodic health monitor still covers
    the wake case when this is unavailable.
    """
    if not IS_WINDOWS:
        return None
    try:
        from ctypes import wintypes
        _CB = ctypes.WINFUNCTYPE(
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
        )

        class _NOTIFY_PARAMS(ctypes.Structure):
            _fields_ = [("Callback", _CB), ("Context", ctypes.c_void_p)]

        powrprof = ctypes.WinDLL("PowrProf.dll")
        register = powrprof.PowerRegisterSuspendResumeNotification
        register.argtypes = [
            wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ]
        unregister = powrprof.PowerUnregisterSuspendResumeNotification
        unregister.argtypes = [ctypes.c_void_p]
        unregister.restype = wintypes.DWORD

        def _cb(_ctx, notify_type, _setting):
            if notify_type in (_PBT_APMRESUMEAUTOMATIC, _PBT_APMRESUMESUSPEND):
                try:
                    set_event()
                except Exception:
                    pass
            return 0

        holder = _CB(_cb)
        params = _NOTIFY_PARAMS(holder, None)  # must outlive the registration
        handle = ctypes.c_void_p()
        if register(_DEVICE_NOTIFY_CALLBACK, ctypes.byref(params), ctypes.byref(handle)) != 0:
            return None
        return {"holder": holder, "params": params,
                "handle": handle, "unregister": unregister}
    except Exception:
        return None


def _unregister_resume_notification(reg):
    """Best-effort unregister — must not leave hidden callbacks behind."""
    if not reg:
        return
    try:
        reg["unregister"](reg["handle"])
    except Exception:
        pass


# Constants for binary management
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "LaptopProxy")
BIN_DIR = os.path.join(CONFIG_DIR, "bin")

# --- v1.5.47: legacy dual-lane ROUTER removed ------------------------------------
# The lane router (v1.5.2, opt-in since v1.5.34 via a flag file) caused more
# failures than it fixed and was never enabled by default. Removed entirely in
# v1.5.47 — tun2socks always dials the phone's 8080 lane directly, with the
# lightweight 8082 standby-lane failover (no router process) kept.
SECONDARY_LANE_PORT = 8082

# --- v1.5.47: fixed default SOCKS5 credentials (identical to the Android side) --------------
# Initial system credentials, IDENTICAL on both ends, never randomized. The
# user may only EDIT them (8-13 alnum chars) in the settings panel — there is
# no manual/auto refresh. These constants must match ProxyServer in the APK.
DEFAULT_PHONE_USER = "linkhelm01"
DEFAULT_PHONE_PASS = "k7h2m9x4qa"
SOCKS_CREDENTIAL_REGEX = re.compile(r"^[A-Za-z0-9]{8,13}$")

# v1.5.35 (R2) / v1.5.36: full-handshake DIAGNOSTIC target. A bare TCP connect
# (L1) says nothing about the SOCKS5 server; v1.5.35 mistakenly GATED health on
# a CONNECT relay here — but the relay egress is the PHONE's network (carriers
# filter TCP/53, some Android stacks restrict CONNECT ports), which bricked
# perfectly healthy tunnels ("cannot connect at all"). v1.5.36: liveness = TCP + greeting;
# the CONNECT relay is executed only by _socks_relay_diag, log-only.
LANE_PROBE_TARGET = ("223.5.5.5", 53)
# v1.5.35 (R3): switch-back hysteresis. The standby lane must be dwelt on for
# at least LANE_B_MIN_DWELL_S, and the primary must pass LANE_A_RECOVER_STRIKES
# consecutive real-handshake probes (≈ LANE_A_RECOVER_STRIKES × 5s window)
# before the tunnel moves back — "switch back the moment it responds" caused flapping rebuilds.
LANE_B_MIN_DWELL_S = 180.0
LANE_A_RECOVER_STRIKES = 6

# --- Minimal hardening (Patch B, see CHANGES.md / DEPENDENCY_LOCK.md) -----------
# Pinned dependency sources. SHA-256 values are of the exact ZIP files from the
# official distribution points and were verified at build time.
TUN2SOCKS_VERSION = "2.7.0"
WINTUN_VERSION = "0.14.1"
TUN2SOCKS_URLS = {
    "amd64": (
        "https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-amd64.zip",
        "c5d46e9452f6c9cc7c15ab9158d6d6a0169ceecd6bca019ce476b49337d2be43",
        "tun2socks-windows-amd64.exe",
    ),
    "arm64": (
        "https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-arm64.zip",
        "74497771068da13f42921adfc540f2abb9ac822404582c8cbe34d30e8c0ea1f5",
        "tun2socks-windows-arm64.exe",
    ),
}
WINTUN_URL = "https://www.wintun.net/builds/wintun-0.14.1.zip"
WINTUN_ZIP_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# The virtual adapter created by this program always uses this gateway, so route
# cleanup can target only routes that belong to LaptopProxy.
OUR_TUN_GATEWAY = "10.0.0.1"

# v1.4.6: silent-elevation scheduled task + stale adapter cleanup.

# v1.5.44: user-gated autostart. The legacy task above used to be re-created
# with an onlogon trigger on EVERY elevated run — boot autostart was not the
# user's choice. Split into two tasks under \LinkHelm\:
#   ElevatedLauncher — NO trigger, only ever started via `schtasks /run`
#                      (keeps the silent-elevation path, never auto-starts);
#   LoginStartup     — onlogon trigger, exists ONLY while the user enabled
#                      "Start after login" in Settings (default OFF).
SCHTASKS_TASK_PATH = "\\LinkHelm\\"
SCHTASKS_LAUNCH_NAME = SCHTASKS_TASK_PATH + "ElevatedLauncher"
SCHTASKS_LOGIN_NAME = SCHTASKS_TASK_PATH + "LoginStartup"
LOGIN_STARTUP_ARG = "--login-startup"

SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")


def load_app_settings():
    """v1.5.44: persisted user preferences (atomic-safe reader).

    Defaults are OFF for anything that changes system behaviour. The Task
    Scheduler real state remains the source of truth for startAtLogon — this
    file only stores the user's preference.
    """
    defaults = {
        "schemaVersion": 1, "startAtLogon": False, "connectOnLaunch": False,
        # UI language: "en" (default) or "zh". Applied at startup.
        "language": "en",
        # v1.5.47: fixed initial credentials (identical on both ends, never
        # randomized). Persisted only after the user edits them in Settings.
        "socksUser": DEFAULT_PHONE_USER, "socksPass": DEFAULT_PHONE_PASS,
    }
    try:
        import json
        with open(SETTINGS_PATH, encoding="utf-8") as fin:
            data = json.load(fin)
        if isinstance(data, dict):
            for key in list(defaults):
                if key in data:
                    defaults[key] = data[key]
    except Exception:
        pass
    # v1.5.47: a persisted credential that no longer passes the 8-13 alnum
    # rule (corrupt edit / older schema) must not break the handshake —
    # fall back to the built-in defaults instead.
    if not (isinstance(defaults["socksUser"], str)
            and SOCKS_CREDENTIAL_REGEX.match(defaults["socksUser"])):
        defaults["socksUser"] = DEFAULT_PHONE_USER
    if not (isinstance(defaults["socksPass"], str)
            and SOCKS_CREDENTIAL_REGEX.match(defaults["socksPass"])):
        defaults["socksPass"] = DEFAULT_PHONE_PASS
    return defaults


def save_app_settings(settings):
    """v1.5.44: atomic JSON write (temp file → fsync → replace)."""
    import json
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fout:
        json.dump(settings, fout, ensure_ascii=False, indent=2)
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(tmp_path, SETTINGS_PATH)


def _cleanup_stale_tun_adapters(log_fn):
    """Remove orphaned LinkHelm* Wintun devices AND their NLA profiles.

    A crashed or force-killed run leaves its Wintun adapter behind; every new
    run then adds another numbered copy ("LinkHelm 14", "... 27") that
    shows as a dead VPN entry forever. pnputil /remove-device (Win10 2004+)
    uninstalls them; failures are ignored — this is cosmetic hygiene.

    v1.4.62: pnputil alone is not enough — Windows keeps every network
    profile under HKLM\\...\\Network\\Profiles forever.

    v1.4.63: the ACTUAL source of the climbing number ("LinkHelm 32",
    "... 41") is the connection name in the NetCfg hive —
    HKLM\\SYSTEM\\CurrentControlSet\\Control\\Network\\{4D36E972-...}\\<GUID>
    \\Connection\\Name. pnputil removes the device but leaves that connection
    entry behind, so every newly created adapter collides with a stale name
    and Windows appends the next number. Sweeping BOTH the NetCfg connection
    entries and the NLA profiles means the fresh adapter is ALWAYS plain
    "LinkHelm" — one stable, unique name. Safe here: this runs after
    the old processes are killed and devices removed, before the new adapter
    is created, so every entry present at this moment is dead history.

    v1.5.42 (P0): the adapter GUID is now FIXED (TUN_ADAPTER_GUID), so the
    sweep must NOT delete our own identity — deleting the fixed-GUID NetCfg
    entry / device on every start forced Windows to recreate them each run
    (registry churn the user measured as "the registry keeps growing"). The sweep now spares
    anything whose PnP instance / NetCfg key carries OUR GUID and only
    removes the legacy random-GUID leftovers ("LinkHelm 2/3/4" era). The
    fixed identity then survives across runs: same device, same NetCfg
    entry, same NLA profile — nothing deleted-and-recreated, nothing grows.
    """
    if not IS_WINDOWS:
        return
    # NetCfg keys and PnP instance IDs carry the GUID without braces.
    our_guid = TUN_ADAPTER_GUID.strip("{}").upper()
    try:
        # v1.5.2: the adapter was renamed LaptopProxyVPN -> LinkHelm. The
        # sweep must cover BOTH names — pre-upgrade leftovers (an old
        # crashed run's "LaptopProxyVPN 41") would otherwise linger in the
        # network list forever.
        ps = (
            # v1.5.42: keep the device that carries OUR fixed GUID —
            # tun2socks will simply reopen it (same identity, config kept).
            "Get-PnpDevice -FriendlyName 'LinkHelm*','LaptopProxyVPN*' | "
            "Where-Object { $_.InstanceId -notlike '*" + our_guid + "*' } | "
            "ForEach-Object { pnputil /remove-device $_.InstanceId } | Out-Null; "
            # v1.4.63: NetCfg connection entries — the numbering source.
            # v1.5.42: keep the key whose PSChildName IS our GUID.
            "Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Network"
            "\\{4D36E972-E325-11CE-BFC1-08002BE10318}' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.PSChildName -ne '" + our_guid + "' -and ( "
            "(Get-ItemProperty -Path ($_.PSPath + '\\Connection') "
            "-Name Name -ErrorAction SilentlyContinue).Name "
            "-like 'LinkHelm*' -or "
            "(Get-ItemProperty -Path ($_.PSPath + '\\Connection') "
            "-Name Name -ErrorAction SilentlyContinue).Name "
            "-like 'LaptopProxyVPN*' ) } | "
            "Remove-Item -Recurse -ErrorAction SilentlyContinue; "
            # v1.4.62: NLA network profiles.
            "Get-ChildItem 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
            "\\Network\\Profiles' -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "$_.GetValue('ProfileName') -like 'LinkHelm*' -or "
            "$_.GetValue('ProfileName') -like 'LaptopProxyVPN*' } | "
            "Remove-Item -Recurse -ErrorAction SilentlyContinue"
        )
        _run_decoded(
            ["powershell", "-NoProfile", "-Command", ps], timeout=60
        )
        log_fn("Cleaned up historical leftover virtual NICs and network config (fixed identity kept; only old random leftovers removed).")
    except Exception:
        pass  # best effort — never block startup on hygiene


def _find_foreign_tun2socks():
    """v1.4.62: image names of any tun2socks process already running.

    Old pre-1.4.5 exes have no process mutex and a crashed run
    can leave its tun2socks child alive. The only reliable cross-version
    signal is the tun2socks child process itself. Returns an empty tuple when
    none is running; errors degrade to empty (never block start).
    """
    if not IS_WINDOWS:
        return ()
    try:
        _, out, _ = _run_decoded(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-Process tun2socks-windows-amd64, tun2socks-windows-arm64"
                " -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty ProcessName",
            ],
            timeout=15,
        )
        return tuple(
            line.strip().lower()
            for line in (out or "").splitlines()
            if line.strip().lower().startswith("tun2socks-")
        )
    except Exception:
        return ()


def _kill_tun2socks_all(log_fn=None):
    """v1.4.63: force-kill EVERY tun2socks process and its children.

    Two call sites share one contract — "the software owns the whole VPN
    lifecycle, nothing tun2socks-related outlives it":
    - TunManager.start(): reconnect takes over. A leftover process (crashed
      run, or one started by an old pre-1.4.5 exe) would keep the old VPN
      alive and its adapter name climbing ("LinkHelm 41"); instead of
      refusing, the new run kills it and proceeds.
    - App window close: TRUE exit. Even if graceful teardown fails, no
      tun2socks child is allowed to outlive the window.
    taskkill /T /F also reaps child processes of the tree. Returns the image
    names that were seen before the kill (may be empty).
    """
    names = _find_foreign_tun2socks()
    if not names:
        return ()
    if log_fn:
        log_fn(f"Force-terminating leftover connection processes ({', '.join(names)})...")
    # taskkill /T /F kills the whole tree, Stop-Process is the fallback;
    # run both, both are best-effort.
    for name in names:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/IM", name],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass
    try:
        _run_decoded(
            [
                "powershell", "-NoProfile", "-Command",
                "Stop-Process -Name tun2socks-windows-amd64,"
                " tun2socks-windows-arm64 -Force -ErrorAction SilentlyContinue",
            ],
            timeout=15,
        )
    except Exception:
        pass
    # v1.4.91: VERIFY death instead of a fixed 0.8s nap. Windows can take a
    # few seconds to actually reap a force-killed process; a reconnect issued
    # in that window saw the old tunnel still holding the TUN adapter ("always
    # occupied, cannot connect"). Poll until the image names are gone (max ~6s).
    leftover = ()
    for _ in range(12):
        leftover = _find_foreign_tun2socks()
        if not leftover:
            break
        time.sleep(0.5)
    if leftover:
        # One more belt-and-suspenders round before giving up.
        for name in leftover:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/IM", name],
                    capture_output=True, timeout=15,
                )
            except Exception:
                pass
        time.sleep(1.0)
        leftover = _find_foreign_tun2socks()
    if log_fn:
        if leftover:
            log_fn(f"Warning: some leftover connection processes could not be terminated ({', '.join(leftover)}).")
        else:
            log_fn("All leftover connection processes have been ended.")
    return names


def _launch_via_scheduled_task():
    """Start the elevated instance through the registered scheduled task.

    v1.5.44: uses the trigger-less \\LinkHelm\\ElevatedLauncher task. It
    carries highest privileges, so this launches a fully elevated instance
    WITHOUT any UAC prompt.
    """
    try:
        rc, _, _ = _run_decoded(
            ["schtasks", "/run", "/tn", SCHTASKS_LAUNCH_NAME], timeout=15
        )
        if rc == 0:
            return True
    except Exception:
        pass
    return False


def _schtasks_task_exists(name):
    try:
        rc, _, _ = _run_decoded(["schtasks", "/query", "/tn", name], timeout=15)
        return rc == 0
    except Exception:
        return False


def _ensure_elevation_task(log_fn=None):
    """(elevated context) Create/refresh the trigger-less elevation task.

    \\LinkHelm\\ElevatedLauncher carries highest privileges but has NO
    trigger — it can only ever be started explicitly via `schtasks /run`,
    so it never auto-starts anything at boot (user-gated autostart, v1.5.44).
    Points at the CURRENT exe so upgrades never launch a stale binary.
    """
    try:
        # PowerShell (not schtasks CLI): only Register-ScheduledTask can
        # create a task without any trigger.
        ps = (
            "$a = New-ScheduledTaskAction -Execute '" + sys.executable + "'; "
            "Register-ScheduledTask -TaskPath '" + SCHTASKS_TASK_PATH + "' "
            "-TaskName 'ElevatedLauncher' -Action $a -RunLevel Highest -Force | Out-Null"
        )
        rc, _, err = _run_decoded(
            ["powershell", "-NoProfile", "-Command", ps], timeout=30
        )
        if rc == 0 and _schtasks_task_exists(SCHTASKS_LAUNCH_NAME):
            if log_fn:
                log_fn("Silent-elevation task ready (does not auto-start at logon).")
            return True
        if log_fn:
            log_fn(f"Warning: silent-elevation task creation failed; the next manual launch will show a UAC prompt. {(err or '').strip()[:120]}")
    except Exception as error:
        if log_fn:
            log_fn(f"Warning: silent-elevation task creation failed; the next manual launch will show a UAC prompt. {error}")
    return False


def _sync_login_startup(enabled, log_fn=None):
    """v1.5.44: make \\LinkHelm\\LoginStartup match the user's choice.

    enabled=True  → create the onlogon task (action = current exe with
                    --login-startup so it starts hidden in the tray);
    enabled=False → delete it. The REAL task state is verified afterwards and
                    is the source of truth shown in Settings.
    Elevation is unaffected: ElevatedLauncher is a separate, trigger-less task.
    """
    try:
        if enabled:
            rc, _, err = _run_decoded(
                ["schtasks", "/create", "/f", "/tn", SCHTASKS_LOGIN_NAME,
                 "/tr", f'"{sys.executable}" {LOGIN_STARTUP_ARG}',
                 "/sc", "onlogon", "/rl", "highest", "/it"],
                timeout=30,
            )
            ok = rc == 0 and _schtasks_task_exists(SCHTASKS_LOGIN_NAME)
            if log_fn:
                log_fn("Autostart enabled (launches after Windows sign-in and minimizes to tray)."
                       if ok else f"Autostart enable failed: {(err or '').strip()[:120]}")
            return ok
        else:
            if _schtasks_task_exists(SCHTASKS_LOGIN_NAME):
                rc, _, err = _run_decoded(
                    ["schtasks", "/delete", "/f", "/tn", SCHTASKS_LOGIN_NAME],
                    timeout=30,
                )
            ok = not _schtasks_task_exists(SCHTASKS_LOGIN_NAME)
            if log_fn:
                log_fn("Autostart disabled (no longer auto-starts after logon; manual launch unaffected)."
                       if ok else f"Autostart disable failed: {(err or '').strip()[:120]}")
            return ok
    except Exception as error:
        if log_fn:
            log_fn(f"Autostart setting change failed: {error}")
        return False


# v1.4.61: link-warmer pacing (see TunManager._link_keepalive_loop).
# Adaptive cadence per user request: 30s while healthy, 15s once degraded;
# 3 consecutive misses at 15s (~45s of continuous unreachability) trigger an
# early rebuild check — the rebuild re-runs the retrying precheck first, so
# transient blips never tear down a working tunnel. The periodic domestic
# site touch (Baidu/Sogou/360 rotation) keeps real end-to-end traffic in-country.
LINK_KEEPALIVE_INTERVAL_OK_S = 30
LINK_KEEPALIVE_INTERVAL_DEGRADED_S = 15
LINK_KEEPALIVE_FAST_STRIKES = 3
DOMESTIC_CHECK_EVERY_PROBES = 10  # ~5 min on a healthy link
DOMESTIC_CHECK_HOSTS = ("www.baidu.com", "www.sogou.com", "www.so.com")

# v1.5.4 (P0): crash-watchdog pacing. Exit is noticed within ~1s (was 5s);
# 3 crashes inside 5 minutes trip FAILED_COOLDOWN — bounded recovery, never
# an endless start-exit-rebuild loop.
CRASH_WATCHDOG_POLL_S = 1
CRASH_COOLDOWN_WINDOW_S = 300
CRASH_COOLDOWN_MAX = 3

# v1.5.4 (P0): bounded post-resume verification window. Wi-Fi Direct needs
# a few seconds after the system wakes; we check 2/5/10s and only rebuild
# when the tunnel is genuinely broken after the whole window.
RESUME_GRACE_DELAYS_S = (2, 5, 10)

# v1.4.4: runtime integrity lock. These are the SHA-256 digests of the official
# binaries bundled with this package (verified against upstream at build time,
# see DEPENDENCY_LOCK.md). Because this program runs with administrator rights,
# an swapped cached binary would be a real privilege-escalation vector, so both
# the installed copies and the bundled copies are re-verified before every use.
EXPECTED_BIN_SHA256 = {
    # v1.5.4: tun2socks v2.7.0 rebuilt from the OFFICIAL v2.7.0 source tag
    # with ONE patched constant (tunnel/tunnel.go tcpConnectTimeout 5s->20s —
    # see DEPENDENCY_LOCK.md). Version string reports
    # "tun2socks-2.7.0 windows/amd64, go1.27.2, 8dda19e-linkhelm".
    # v1.5.4: the legacy v2.5.2 backup binary is REMOVED — the ONLY allowed
    # recovery source is the bundled v2.7.0 build verified below, and a
    # runtime `--version` check (P0-1) refuses any binary that does not
    # report that exact build. The unpatched official v2.7.0 (076b3c3d...)
    # stays out of the package as a build-time reference only.
    # wintun.dll unchanged (0.14.1).
    "tun2socks.exe": "9c27b029f751fccda8f8c948f21f0a2c7676752ae207215d4bc5b461961be70b",
    "wintun.dll": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
}

# v1.5.4 (P0-1): the exact build marker our pinned tun2socks must report.
# Hash alone proves bytes; this proves *which* build the bytes are.
TUN2SOCKS_BUILD_MARK = "8dda19e-linkhelm"

# v1.5.41 (P0): FIXED Wintun adapter GUID. tun2socks v2.7.0 natively supports
# this via the device URL query (`tun://<name>?guid=<guid>` sets
# WintunStaticRequestedGUID, engine/parse_windows.go) — a pure CLI-level
# setting: no driver swap, no tun2socks core change. With a stable GUID
# Windows keeps ONE network identity for the adapter: no "LinkHelm 2/3/4"
# renumbering, no fresh "unidentified network" profile on every start/stop
# (the source of the OS-level notification spam), and IP/DNS config reuse.
TUN_ADAPTER_GUID = "{F4E4A1B2-9C3D-4E5F-8A7B-6C5D4E3F2A1B}"


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fin:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_bin_pair(t2s_path, wt_path):
    """Verify both binaries against the integrity lock.

    Returns (ok, bad_name_or_None); missing files count as failures so callers
    can fall back to the bundled copies or a pinned download.
    """
    for name, path in (("tun2socks.exe", t2s_path), ("wintun.dll", wt_path)):
        if not os.path.exists(path) or _file_sha256(path) != EXPECTED_BIN_SHA256[name]:
            return False, name
    return True, None


def _bundled_bin_dir():
    """Directory holding the build-time-verified binaries shipped with the package."""
    base = getattr(sys, "_MEIPASS", None)
    if base and os.path.isdir(os.path.join(base, "bin")):
        return os.path.join(base, "bin")
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundled_bin")
    if os.path.isdir(local):
        return local
    return None


def _tun2socks_version_ok(t2s_path):
    """v1.5.4 (P0-1): confirm the executing binary IS our pinned v2.7.0 build.

    Runs `tun2socks.exe --version` and requires both the 2.7.0 version and
    the LinkHelm build marker. A hash match already proves the bytes; this
    additionally catches a stale %APPDATA% cache (e.g. a leftover 2.5.2)
    that somehow passes, and makes the field log name the exact build.
    """
    try:
        _, out, _ = _run_decoded([t2s_path, "--version"], timeout=10)
    except Exception:
        return False
    text = " ".join((out or "").split())
    return "2.7.0" in text and TUN2SOCKS_BUILD_MARK in text


def socks5_greet(conn, socks_user=None, socks_pass=None, timeout=3.0):
    """v1.5.47: shared SOCKS5 greeting + optional RFC 1929 userpass auth.

    Offers [no-auth, userpass]. If the server selects userpass (0x02) the
    credentials are sent; credentials missing → auth failure. Returns
    "SOCKS5_OK" or an error string; conn must already be connected.
    """
    try:
        if socks_user is not None and socks_pass is not None:
            conn.sendall(b"\x05\x02\x00\x02")
        else:
            conn.sendall(b"\x05\x01\x00")
        reply = b""
        while len(reply) < 2:
            part = conn.recv(2 - len(reply))
            if not part:
                return "SOCKS5_SHORT_REPLY"
            reply += part
        if reply[0:1] != b"\x05":
            return "SOCKS5_BAD_REPLY"
        if reply[1:2] == b"\x00":
            return "SOCKS5_OK"
        if reply[1:2] == b"\x02":
            if not socks_user or not socks_pass:
                return "SOCKS5_AUTH_REQUIRED"
            user_b = socks_user.encode("utf-8")
            pass_b = socks_pass.encode("utf-8")
            conn.sendall(
                b"\x01" + bytes([len(user_b)]) + user_b
                + bytes([len(pass_b)]) + pass_b
            )
            sub = b""
            while len(sub) < 2:
                part = conn.recv(2 - len(sub))
                if not part:
                    return "SOCKS5_SHORT_REPLY"
                sub += part
            if sub == b"\x01\x00":
                return "SOCKS5_OK"
            if sub[0:1] == b"\x01":
                return "SOCKS5_AUTH_FAILED"
            return "SOCKS5_BAD_REPLY"
        if reply[1:2] == b"\xff":
            return "SOCKS5_NO_ACCEPTABLE_METHOD"
        return "SOCKS5_BAD_REPLY"
    except socket.timeout:
        return "SOCKS5_HANDSHAKE_TIMEOUT"
    except OSError as exc:
        return f"SOCKS5_IO_FAILED({type(exc).__name__})"


def check_socks5(host, port, timeout=3.0, socks_user=None, socks_pass=None):
    """v1.3 preflight before Wintun/route changes, with separate TCP/SOCKS errors.

    All outcomes are safe: this function only opens a TCP connection and performs
    the SOCKS5 greeting (no target CONNECT, no privileged system modifications).
    v1.5.47: supports RFC 1929 userpass auth (must match the phone's policy).
    """
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return "INVALID_TARGET"
    if not host or not 0 < port_num < 65536:
        return "INVALID_TARGET"

    try:
        conn = socket.create_connection((host, port_num), timeout=timeout)
    except socket.timeout:
        return "TCP_CONNECT_TIMEOUT"
    except OSError as exc:
        return f"TCP_CONNECT_FAILED({type(exc).__name__})"

    with conn:
        try:
            conn.settimeout(timeout)
            return socks5_greet(conn, socks_user, socks_pass, timeout=timeout)
        except socket.timeout:
            return "SOCKS5_HANDSHAKE_TIMEOUT"
        except OSError as exc:
            return f"SOCKS5_IO_FAILED({type(exc).__name__})"


# v1.4.2 stability: Wi-Fi Direct power-save can swallow a single connect attempt
# (field log: tun2socks "i/o timeout" blips to 192.168.49.1:8080). One blip must
# never fail a whole VPN start or trigger a tunnel rebuild, so the precheck and
# the health monitor now tolerate transient timeouts with retries/confirmation.
TRANSIENT_PRECHECK_PREFIXES = ("TCP_CONNECT_FAILED", "SOCKS5_IO_FAILED")
TRANSIENT_PRECHECK_RESULTS = {"TCP_CONNECT_TIMEOUT", "SOCKS5_HANDSHAKE_TIMEOUT"}


def is_transient_precheck(result):
    return result in TRANSIENT_PRECHECK_RESULTS or result.startswith(
        TRANSIENT_PRECHECK_PREFIXES
    )


def tun2socks_cmd(t2s_exe, proxy_url):
    """Build the tun2socks launch command (v1.5.37).

    tun2socks v2.7.0 switched its CLI from Go's stdlib flag to pflag: long
    flags now REQUIRE the double-dash form. The old single-dash long form
    (`-loglevel`) is parsed as shorthand `-l` + garbage → "unknown shorthand
    flag: 'l'" → the process prints usage and exits → "exited before its
    adapter was ready". v2.5.2 accepted both spellings, so use the pflag-safe
    long form (`--device/--proxy/--loglevel/--tcp-auto-tuning`) which is also
    valid on v2.5.2 — one command shape for every bundled version.
    """
    return [
        t2s_exe,
        # v1.5.41: fixed GUID — same adapter identity every run (see
        # TUN_ADAPTER_GUID). tun2socks v2.7.0 reads `guid` from the device
        # URL query; v2.5.2 ignores unknown query params, so the shape is
        # safe across bundled versions.
        "--device", f"tun://LinkHelm?guid={TUN_ADAPTER_GUID}",
        "--proxy", proxy_url,
        "--loglevel", "warning",
        # v1.4.61: netstack TCP receive-buffer auto-tuning — keeps long
        # sessions responsive instead of degrading after hours of uptime.
        "--tcp-auto-tuning",
    ]


def check_socks5_with_retry(host, port, attempts=4, delay=2.0, timeout=6.0,
                            log_fn=None, sleep_fn=time.sleep,
                            socks_user=None, socks_pass=None):
    """Precheck with retries for transient Wi-Fi Direct blips.

    v1.5.3: defaults widened from 3×4s to 4×6s — the field log showed all
    three 4s attempts landing inside one Wi-Fi Direct power-save wake
    (3-8s), producing "repeated failed reconnects" until the radio finally woke.

    - SOCKS5_OK → return immediately.
    - Transient failures (timeout / connection reset / IO) → wait `delay` and retry,
      up to `attempts` times.
    - Permanent failures (bad reply, method refused, auth failed, invalid target) → fail fast.
    All outcomes remain safe: TCP + SOCKS5 greeting only, no system changes.
    """
    result = check_socks5(host, port, timeout=timeout,
                          socks_user=socks_user, socks_pass=socks_pass)
    if result == "SOCKS5_OK" or not is_transient_precheck(result):
        return result
    for attempt in range(2, attempts + 1):
        if log_fn:
            log_fn(
                f"SOCKS5 pre-check attempt {attempt - 1}/{attempts} failed ({result}),"
                f"retrying in {delay:.0f}s..."
            )
        sleep_fn(delay)
        result = check_socks5(host, port, timeout=timeout,
                              socks_user=socks_user, socks_pass=socks_pass)
        if result == "SOCKS5_OK" or not is_transient_precheck(result):
            return result
    return result


def _build_dns_query(qid, name="example.com"):
    """v1.4.3: minimal DNS A query for the UDP reachability check."""
    out = bytearray()
    out += qid.to_bytes(2, "big")
    out += b"\x01\x00"          # flags: RD=1
    out += b"\x00\x01"          # QDCOUNT=1
    out += b"\x00" * 6          # AN/NS/ARCOUNT=0
    for label in name.split("."):
        out.append(len(label))
        out += label.encode("idna") if not label.isascii() else label.encode()
    out += b"\x00"              # root
    out += b"\x00\x01\x00\x01"  # QTYPE=A QCLASS=IN
    return bytes(out)


# v1.4.93: UDP/DNS probe pool. The phone's upstream decides which resolvers
# actually answer over its cellular/wi-fi UDP path — a fixed dual-DNS list
# still missed carriers that close one of them (real device: UDP handshake to
# 223.5.5.5 "forcibly closed by remote host"). Order matters: 119.29.29.29
# stays first (user-confirmed phone upstream); 114 is a widely-accepted
# carrier-friendly fallback. The first probe that succeeds becomes the
# sticky primary for the rest of the session (see _do_udp_check).
UDP_CHECK_DNS_POOL = ("119.29.29.29", "223.5.5.5", "114.114.114.114")


def socks5_rep_meaning(rep):
    """beta1.0.3: human-readable meaning of a SOCKS5 CONNECT reply code (RFC 1928 §6),
    so the desktop log reports WHY the host refused instead of a bare hex code."""
    return {
        0x01: tr("general failure (host-side error; check the phone log)",
                 "一般失败（主端内部错误，请看手机端日志）"),
        0x02: tr("not allowed by the host rules (blocked or quota-rejected)",
                 "主端规则不允许（被踢除或配额拒绝）"),
        0x03: tr("network unreachable from the phone", "主端侧网络不可达"),
        0x04: tr("target host unreachable from the phone", "主端无法连到目标主机"),
        0x05: tr("target refused by the phone's upstream", "目标连接被主端上游拒绝"),
        0x06: tr("TTL expired", "TTL 超时"),
        0x07: tr("command not supported", "命令不支持"),
        0x08: tr("address type not supported", "地址类型不支持"),
    }.get(rep, tr("unknown reply", "未知应答"))


def socks5_udp_dns_check(host, port, dns_ip="223.5.5.5", dns_port=53, timeout=5.0,
                         socks_user=None, socks_pass=None):
    """v1.4.3: UDP connectivity check — UDP ASSOCIATE to the phone SOCKS5, DNS query via the relay.

    Covers both chain segments: local->phone (UDP over Wi-Fi Direct) + phone->internet (UDP egress).
    This is exactly the path game and other UDP traffic takes. Returns (True, description) / (False, reason);
    opens only one TCP control connection plus UDP probe packets, makes no system changes.
    """
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return False, "invalid port"
    try:
        ctrl = socket.create_connection((host, port_num), timeout=timeout)
    except socket.timeout:
        return False, "TCP control connection timed out"
    except OSError as exc:
        return False, f"TCP control connection failed ({type(exc).__name__})"

    relay = None
    try:
        ctrl.settimeout(timeout)
        # v1.5.47: greeting + optional RFC 1929 auth (same policy as TCP precheck).
        greet = socks5_greet(ctrl, socks_user, socks_pass, timeout=timeout)
        if greet != "SOCKS5_OK":
            reason = {
                "SOCKS5_AUTH_REQUIRED": "server requires credentials but none were supplied",
                "SOCKS5_AUTH_FAILED": "username or password is incorrect",
                "SOCKS5_NO_ACCEPTABLE_METHOD": "SOCKS5 method rejected",
                "SOCKS5_HANDSHAKE_TIMEOUT": "SOCKS5 handshake timed out",
            }.get(greet, f"SOCKS5 handshake failed ({greet})")
            return False, reason

        # UDP ASSOCIATE（ATYP=1, 0.0.0.0:0）
        ctrl.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        head = b""
        while len(head) < 4:
            part = ctrl.recv(4 - len(head))
            if not part:
                return False, "UDP ASSOCIATE response incomplete"
            head += part
        if head[0] != 5:
            return False, "UDP ASSOCIATE version error"
        if head[1] != 0:
            return False, f"UDP ASSOCIATE rejected (reply=0x{head[1]:02x})"
        atyp = head[3]
        if atyp == 1:
            bnd = b""
            while len(bnd) < 4:
                bnd += ctrl.recv(4 - len(bnd))
        elif atyp == 3:
            ln = ctrl.recv(1)
            if not ln:
                return False, "UDP ASSOCIATE domain length missing"
            bnd = b""
            while len(bnd) < ln[0]:
                bnd += ctrl.recv(ln[0] - len(bnd))
        elif atyp == 4:
            bnd = b""
            while len(bnd) < 16:
                bnd += ctrl.recv(16 - len(bnd))
        else:
            return False, f"UDP ASSOCIATE unexpected ATYP 0x{atyp:02x}"
        pbuf = b""
        while len(pbuf) < 2:
            pbuf += ctrl.recv(2 - len(pbuf))
        relay_port = (pbuf[0] << 8) | pbuf[1]
        if relay_port <= 0:
            return False, "UDP relay port invalid"
        relay_host = host if bnd == b"\x00\x00\x00\x00" else socket.inet_ntoa(bnd)

        # UDP data plane: RSV FRAG ATYP ADDR PORT + DNS query
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay.settimeout(timeout)
        qid = random.randint(1, 0xFFFF)
        query = _build_dns_query(qid)
        dns_ip_bytes = socket.inet_aton(dns_ip)
        datagram = b"\x00\x00\x00" + b"\x01" + dns_ip_bytes + dns_port.to_bytes(2, "big") + query
        relay.sendto(datagram, (relay_host, relay_port))
        data, _addr = relay.recvfrom(2048)
        if len(data) < 10 + 12:
            return False, "UDP relay response too short"
        payload = data[10:]
        resp_id = (payload[0] << 8) | payload[1]
        if resp_id != qid or not (payload[2] & 0x80):
            return False, "UDP received but not a valid DNS response"
        return True, (
            f"UDP full path reachable (DNS query {dns_ip}:{dns_port} completed via phone relay, "
            f"relay port {relay_port})"
        )
    except socket.timeout:
        return False, f"UDP timed out (no response within {timeout:.0f}s)"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            relay.close()
        except Exception:
            pass
        try:
            ctrl.close()  # close the control connection -> the phone side reclaims the UDP association
        except Exception:
            pass


def _run_decoded(cmd, timeout=None):
    """v1.3.2: run a command and decode its output regardless of console code page.

    Localized Windows tools (netsh/route/ping on zh-CN) emit GBK bytes. text=True
    with a UTF-8 locale crashes the subprocess reader thread and silently loses
    the output — the field symptom was "Failed to create Virtual Adapter" even
    though the adapter existed (netsh output was lost to UnicodeDecodeError).
    Decode bytes as UTF-8 first, then the ANSI code page (mbcs); never raise on
    stray bytes. Returns (returncode, stdout_str, stderr_str).
    """
    res = subprocess.run(cmd, capture_output=True, timeout=timeout)

    def _dec(data):
        if not data:
            return ""
        for encoding in ("utf-8", "mbcs"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    return res.returncode, _dec(res.stdout), _dec(res.stderr)


def ping_host(host, timeout_ms=1500):
    """v1.3.1: best-effort single ICMP echo. True only on a real reply.

    Reads raw bytes on purpose: localized ping output (e.g. zh-CN GBK) crashes
    text-mode decode with UnicodeDecodeError (field bug, byte 0xd5/0xd3).
    """
    if not IS_WINDOWS:
        return False
    try:
        res = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            timeout=(timeout_ms / 1000.0) + 2,
            creationflags=0x08000000,
        )
    except Exception:
        return False
    if res.returncode != 0:
        return False
    # zh-CN and en-US replies both carry "TTL=<n>"; timeout/loss output does not.
    return b"TTL=" in (res.stdout or b"").upper()


def diagnose_tcp_failure(host, precheck):
    """v1.3.1: explain TCP-layer precheck failures with an ICMP cross-check.

    Covers the exact field scenario 'PingSucceeded=True / TcpTestSucceeded=False'
    (Test-NetConnection): the Wi-Fi Direct network is up, but nothing is
    listening on the phone's proxy port.
    """
    if not (precheck.startswith("TCP_CONNECT_FAILED") or precheck == "TCP_CONNECT_TIMEOUT"):
        return ""
    if ping_host(host):
        return ("Network reachable (ping OK) but the phone proxy port is not listening (ICMP works, TCP closed)."
                f"On the phone, confirm the status card shows READY/SHARING ({host});"
                "if it shows FAILED/Waiting, stop and restart sharing on the phone.")
    return (f"ping {host} also fails: confirm the PC is connected to the phone's hotspot/Wi-Fi Direct network, "
            "and that sharing is enabled on the phone.")

class TunManager:
    """
    Manages tun2socks & wintun.dll.
    Creates a Virtual Network Adapter to capture 100% of Windows traffic.
    """
    # v1.5.47: class-level credential defaults so tests / subclasses that
    # bypass __init__ still see defined values (mirrors the old _dual_lane
    # class-default pattern).
    socks_user = DEFAULT_PHONE_USER
    socks_pass = DEFAULT_PHONE_PASS

    def __init__(self, phone_ip, phone_port, local_port, log_fn, on_tunnel_lost=None,
                 confirm_download=None, socks_user=None, socks_pass=None):
        self.on_tunnel_lost = on_tunnel_lost
        self.phone_ip = phone_ip
        self.phone_port = phone_port
        self.local_port = local_port
        # v1.5.47: SOCKS5 credentials dialled by every probe + tun2socks.
        self.socks_user = socks_user or DEFAULT_PHONE_USER
        self.socks_pass = socks_pass or DEFAULT_PHONE_PASS
        self.log = log_fn
        self.process = None
        self._monitoring_active = False
        self._connection_healthy = False
        self._restarting = False
        # Patch B: optional callback consulted before any dependency download.
        self.confirm_download = confirm_download
        self._owned_routes = []
        # Round-2 fix (P1-W2): gateway of the host route back to the phone, so cleanup
        # can remove exactly what we added.
        self._phone_route_gw = None
        # v1.5.34: lightweight lane FAILOVER (no router, no 19:1 spreading).
        # tun2socks dials the primary lane (8080) directly — the proven 1.4.93
        # data plane. Only when the primary times out on several consecutive
        # short probes does the tunnel move to the standby 8082 lane, and it
        # moves back the moment the primary answers again.
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        # v1.5.35 (R3): switch-back hysteresis state.
        self._lane_b_since = 0.0
        self._lane_a_recover_strikes = 0
        # v1.5.4 (P0): crash-watchdog cooldown state + suspend/resume event.
        self._crash_times = []
        self._resume_event = threading.Event()
        self._resume_reg = None

    def _clean_routes(self):
        """Patch B / Round-2: only remove routes that belong to this program.        The old implementation unconditionally deleted 0.0.0.0/1 and 128.0.0.0/1,
        which could break other VPN software running on the machine. All routes we
        create point at OUR_TUN_GATEWAY (the LinkHelm adapter) or at the saved
        return-route gateway, so cleanup targets exactly those — including stale
        leftovers from a previous crash.
        """
        for dest in ("0.0.0.0", "128.0.0.0"):
            if self._find_route_gateways(dest, "128.0.0.0"):
                self.log(f"Removing route {dest}/1 via {OUR_TUN_GATEWAY} (added by LaptopProxy)...")
                subprocess.run(
                    ["route", "delete", dest, "mask", "128.0.0.0", OUR_TUN_GATEWAY],
                    capture_output=True,
                )
        # Round-2: remove the host route back to the phone (only ours, via saved gw).
        phone_gw = self._phone_route_gw
        if phone_gw and self.phone_ip:
            self.log(f"Removing return route to {self.phone_ip}/32 via {phone_gw}...")
            subprocess.run(
                ["route", "delete", self.phone_ip, "mask", "255.255.255.255", phone_gw],
                capture_output=True,
            )
        self._phone_route_gw = None
        self._owned_routes = []

    def _find_route_gateways(self, dest, mask):
        """Return the list of gateways currently serving dest/mask (empty = none)."""
        try:
            # v1.3.2: _run_decoded — text=True lost GBK "route print" output on
            # zh-CN Windows, silently disabling route ownership detection.
            _, out, _ = _run_decoded(["route", "print", "-4"], timeout=15)
        except Exception:
            return []
        if not out:
            return []
        gateways = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == dest and parts[1] == mask:
                gateways.append(parts[2])
        return gateways

    def _wait_adapter_ip_ready(self, timeout_s):
        """v1.5.46: poll until the LinkHelm adapter reports 10.0.0.2.

        With the fixed-GUID adapter reused across runs (v1.5.41+) this is
        typically instant, so rebuilds can skip the old blind 2.0s+1.0s
        sleeps — measured rebuild path shrinks by ~3s. Returns False on
        timeout or if tun2socks exited early.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False
            _, out, _ = _run_decoded(
                ["netsh", "interface", "ipv4", "show", "addresses", "name=LinkHelm"],
                timeout=15,
            )
            if "10.0.0.2" in (out or ""):
                return True
            time.sleep(0.25)
        return False

    def _configure_adapter_if_needed(self):
        """v1.5.41 (P0): idempotent adapter config — reuse what is correct.

        With the fixed adapter GUID (TUN_ADAPTER_GUID) Windows keeps the
        interface's static IP/DNS across runs, so re-running `netsh set` on
        every start is wasted work AND extra churn on the network stack
        (each set can bounce NLA state). Check first:
        - address already 10.0.0.2/24 via 10.0.0.1 → skip `set address`
        - DNS already 223.5.5.5 (+119.29.29.29)   → skip `set/add dns`
        Only genuinely missing config is applied. Never raises: a failed
        config application must surface via the follow-up probe, not here.
        """
        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "addresses", "name=LinkHelm"],
            timeout=15,
        )
        if "10.0.0.2" in (out or ""):
            self.log("NIC IP is already 10.0.0.2/24; reusing existing config (skipped netsh set address).")
        else:
            self.log("Configuring IP...")
            subprocess.run(
                ["netsh", "interface", "ip", "set", "address", "name=LinkHelm",
                 "static", "10.0.0.2", "255.255.255.0", "10.0.0.1"],
                check=True, capture_output=True,
            )

        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "dnsservers", "name=LinkHelm"],
            timeout=15,
        )
        dns_out = (out or "")
        if "223.5.5.5" in dns_out and "119.29.29.29" in dns_out:
            self.log("NIC DNS is already AliDNS/DNSPod; reusing existing config (skipped netsh set dns).")
        else:
            self.log("Configuring DNS...")
            # v1.4.61: in-country resolvers only — 8.8.8.8 sent EVERY domain
            # lookup abroad (the single biggest recurring cross-border traffic source, a
            # needless detection footprint on managed networks). AliDNS +
            # DNSPod are fast and domestic; resolution still travels the
            # same tunnel path.
            subprocess.run(
                ["netsh", "interface", "ip", "set", "dns", "name=LinkHelm",
                 "static", "223.5.5.5"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["netsh", "interface", "ip", "add", "dns", "name=LinkHelm",
                 "119.29.29.29", "index=2"],
                check=False, capture_output=True,
            )

    def _add_owned_routes(self):
        """Patch B / Round-2: add the return route to the phone plus the two /1
        catch-all routes, tracking exactly what we create.

        Routes that already exist (e.g. created by other VPN software) are left
        untouched and reported; the rollback path (_clean_routes) only ever removes
        routes pointing at our own TUN gateway or the saved phone return-route gw.
        """
        self._owned_routes = []

        # Round-2 (P1-W2): pin a host route to the phone through the original default
        # gateway BEFORE the /1 routes take over, otherwise the proxy/health-check
        # connections to the phone would be captured by our own TUN (routing loop).
        default_gws = self._find_route_gateways("0.0.0.0", "0.0.0.0")
        phone_gw = default_gws[0] if default_gws else None
        if phone_gw and self.phone_ip:
            if self._find_route_gateways(self.phone_ip, "255.255.255.255"):
                self.log("Return route to phone already exists; not modified.")
            else:
                rc, out, err = _run_decoded(
                    ["route", "add", self.phone_ip, "mask", "255.255.255.255", phone_gw, "metric", "1"],
                    timeout=15,
                )
                if rc == 0:
                    self._phone_route_gw = phone_gw
                    self.log(f"Return route to phone pinned via {phone_gw}.")
                else:
                    detail = (err or out or "").strip()
                    self.log(f"Warning: could not pin return route to phone via {phone_gw}: {detail}")

        for dest in ("0.0.0.0", "128.0.0.0"):
            existing = self._find_route_gateways(dest, "128.0.0.0")
            if OUR_TUN_GATEWAY in existing:
                # Stale route from a previous (possibly crashed) run of this program.
                subprocess.run(
                    ["route", "delete", dest, "mask", "128.0.0.0", OUR_TUN_GATEWAY],
                    capture_output=True,
                )
                existing = self._find_route_gateways(dest, "128.0.0.0")
            if existing:
                self.log(
                    f"Route {dest}/1 already exists via {', '.join(existing)} "
                    "(another VPN?); it will not be modified."
                )
                continue
            rc, out, err = _run_decoded(
                ["route", "add", dest, "mask", "128.0.0.0", OUR_TUN_GATEWAY, "metric", "1"],
                timeout=15,
            )
            if rc != 0:
                self._clean_routes()
                detail = (err or out or "").strip()
                raise RuntimeError(f"route add {dest}/1 failed: {detail}")
            self._owned_routes.append(dest)

    def _check_dependencies(self):
        os.makedirs(BIN_DIR, exist_ok=True)
        t2s_path = os.path.join(BIN_DIR, "tun2socks.exe")
        wt_path  = os.path.join(BIN_DIR, "wintun.dll")

        # v1.5.4: sweep legacy per-version backups out of the runtime cache
        # (e.g. a leftover tun2socks_v2.5.2_backup.exe from an old installer).
        # The cache must only ever contain the two pinned files below.
        try:
            for stale in os.listdir(BIN_DIR):
                if stale.startswith("tun2socks_") and stale.endswith(".exe"):
                    try:
                        os.remove(os.path.join(BIN_DIR, stale))
                        self.log(f"Cleaned legacy cache component: {stale}")
                    except OSError:
                        pass
        except OSError:
            pass

        # v1.4.4: verify whatever is already installed before trusting it. This
        # program runs elevated, so a replaced cached binary must never execute.
        # v1.5.4: the message now states the ONLY recovery source (the bundled
        # v2.7.0 build) — no 2.5.2 or any other fallback exists any more.
        for path, name in ((t2s_path, "tun2socks.exe"), (wt_path, "wintun.dll")):
            if os.path.exists(path) and _file_sha256(path) != EXPECTED_BIN_SHA256[name]:
                self.log(
                    f"Security warning: {name} does not match the pinned SHA-256 (may be replaced or an old cached copy), "
                    "removed; reinstalling only from the pinned v2.7.0 build."
                )
                try:
                    os.remove(path)
                except OSError as error:
                    self.log(f"Could not remove {name} (verification failed): {error}")

        if os.path.exists(t2s_path) and os.path.exists(wt_path):
            # v1.5.4 (P0-1): runtime version gate on the cached pair too.
            if _tun2socks_version_ok(t2s_path):
                return t2s_path
            self.log("Cached tun2socks.exe version check failed (not the pinned v2.7.0 build); reinstalling the bundled version.")
            try:
                os.remove(t2s_path)
            except OSError:
                pass

        # Patch B (preferred path): use the copies bundled with the onedir package.
        # Re-verified at every launch (v1.4.4), not just at build time.
        bundled = _bundled_bin_dir()
        if bundled:
            bundled_t2s = os.path.join(bundled, "tun2socks.exe")
            bundled_wt = os.path.join(bundled, "wintun.dll")
            ok, bad = _verify_bin_pair(bundled_t2s, bundled_wt)
            if ok:
                self.log("Using bundled network components (SHA-256 re-verified at startup)...")
                self._install_bundled_file(bundled_t2s, t2s_path)
                self._install_bundled_file(bundled_wt, wt_path)
                installed_ok, installed_bad = _verify_bin_pair(t2s_path, wt_path)
                # v1.5.4 (P0-1): hash + runtime version double gate.
                if installed_ok and _tun2socks_version_ok(t2s_path):
                    self.log(
                        f"tun2socks version verified: v{TUN2SOCKS_VERSION} "
                        f"pinned build {TUN2SOCKS_BUILD_MARK}."
                    )
                    return t2s_path
                self.log(f"Error: {installed_bad or 'tun2socks.exe'} still failed verification after install; will try the official download.")
            else:
                self.log(f"Error: bundled {bad} failed hash verification; local copy not trusted, will try official download.")

        # Determine architecture
        arch = platform.machine().lower()
        if 'arm' in arch or 'aarch' in arch:
            t2s_url, t2s_sha, t2s_member = TUN2SOCKS_URLS["arm64"]
            wt_arch = "arm64"
        else:
            t2s_url, t2s_sha, t2s_member = TUN2SOCKS_URLS["amd64"]
            wt_arch = "amd64"
        wt_member = f"wintun/bin/{wt_arch}/wintun.dll"

        # Patch B: downloads must be explicitly confirmed by the user.
        if self.confirm_download is None:
            raise RuntimeError(
                "Bundled network components are missing and downloads require explicit "
                "user confirmation. Reinstall the package or approve the download."
            )
        if not self.confirm_download():
            raise RuntimeError("Dependency download cancelled by user.")

        self.log("Downloading network components via the bridge...")
        # Start a temporary bridge for downloading
        temp_bridge = HttpSocksBridge(self.local_port, self.phone_ip, self.phone_port, self.log)
        temp_bridge.start()
        time.sleep(1.5)

        proxy_handler = urllib.request.ProxyHandler({
            'http': f'http://127.0.0.1:{self.local_port}',
            'https': f'http://127.0.0.1:{self.local_port}'
        })
        # Patch B: local opener only — never change the global URL opener that other
        # threads in this process rely on.
        opener = urllib.request.build_opener(proxy_handler)

        try:
            if not os.path.exists(t2s_path):
                self.log("Downloading tun2socks...")
                self._download_and_extract(opener, t2s_url, t2s_sha, t2s_member, t2s_path)

            if not os.path.exists(wt_path):
                self.log("Downloading wintun.dll...")
                self._download_and_extract(opener, WINTUN_URL, WINTUN_ZIP_SHA256, wt_member, wt_path)
            self.log("Dependencies downloaded and verified.")
        except Exception as e:
            self.log(f"Download failed: {e}")
            raise e
        finally:
            temp_bridge.stop()
            time.sleep(0.5)

        return t2s_path

    @staticmethod
    def _install_bundled_file(src, dest):
        tmp = dest + ".tmp"
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            fout.write(fin.read())
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dest)

    @staticmethod
    def _download_and_extract(opener, url, expected_sha256, member_name, dest_path):
        """Download a pinned archive, verify SHA-256 and the exact zip member, then
        atomically install it. Any mismatch leaves the destination untouched."""
        response = opener.open(url, timeout=60)
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"Download from {url} exceeds the size limit; rejected.")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}. "
                "The file was NOT installed."
            )
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if member_name not in z.namelist():
                raise RuntimeError(f"Expected member {member_name!r} not found in {url}.")
            payload = z.read(member_name)
        tmp = dest_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest_path)

    def start(self):
        # v1.4.63: reconnect TAKES OVER. A leftover tun2socks process (from a
        # crashed run, or started by an old pre-1.4.5 exe that has no mutex)
        # keeps the old VPN alive and its adapter name climbing. Kill it and
        # proceed — refusing (v1.4.62) read to the user as "reconnect does
        # nothing". Only if the kill itself fails do we stop.
        _kill_tun2socks_all(log_fn=self.log)
        if _find_foreign_tun2socks():
            raise RuntimeError(
                "Leftover connection processes could not be ended; a new connection cannot be started safely.\n"
                "Open Task Manager, end tun2socks-windows-amd64.exe, then retry."
            )
        # Round-2 fix (P0-W1): verify the phone's SOCKS5 endpoint BEFORE touching
        # Wintun, IP configuration or routes. A phone-side failure now surfaces as a
        # precise error instead of a mysterious TUN/routing fault later.
        # v1.4.2: retry transient timeouts — one power-save blip must not abort startup.
        precheck = check_socks5_with_retry(
            self.phone_ip, self.phone_port, log_fn=self.log,
            socks_user=self.socks_user, socks_pass=self.socks_pass
        )
        if precheck != "SOCKS5_OK":
            diagnosis = diagnose_tcp_failure(self.phone_ip, precheck)
            if diagnosis:
                self.log(f"Diagnostics: {diagnosis}")
            raise RuntimeError(
                f"Phone proxy precheck failed: {precheck} "
                f"(target {self.phone_ip}:{self.phone_port}). "
                "Wintun/routes were NOT modified. Check that the host phone is sharing."
            )
        self.log(f"SOCKS5 pre-check passed ({self.phone_ip}:{self.phone_port}).")

        # v1.4.6: remove orphaned LinkHelm* adapters left by crashed/killed
        # runs — otherwise Windows keeps piling up "LinkHelm 14/27/..."
        # dead entries in the network list. Best effort; the fresh adapter for
        # this run is created right after.
        _cleanup_stale_tun_adapters(self.log)

        t2s_exe = self._check_dependencies()

        self.log("Starting the LinkHelm adapter...")
        # v1.5.47: dual-lane router removed — tun2socks dials the phone directly
        # (lightweight 8082 failover lives in the health monitor).
        cmd = tun2socks_cmd(t2s_exe, self._tun2socks_proxy_url())

        # Hide window on Windows, capture output
        cflags = 0x08000000 if IS_WINDOWS else 0
        self.process = subprocess.Popen(cmd, cwd=BIN_DIR, creationflags=cflags,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._read_tun2socks_output()

        self.log("Waiting for the adapter to be ready...")
        adapter_found = False
        for _ in range(15):
            if self.process.poll() is not None:
                raise RuntimeError("tun2socks exited before its adapter was ready.")
            # v1.3.2: _run_decoded — text=True crashed on zh-CN GBK netsh output,
            # so the adapter was never detected and startup rolled back even
            # though tun2socks had created it successfully.
            _, out, _ = _run_decoded(["netsh", "interface", "ipv4", "show", "interfaces"], timeout=15)
            if "LinkHelm" in out:
                adapter_found = True
                break
            time.sleep(1)
            
        if not adapter_found:
            self.stop()
            raise Exception("Failed to create Virtual Adapter.")

        # v1.5.46: fixed-GUID adapter is usually reused — poll for its IP
        # instead of the old blind 2.0s+1.0s sleeps (faster reconnect).
        if not self._wait_adapter_ip_ready(5.0):
            self.log("Adapter IP not ready immediately; falling back to a fixed wait.")
            time.sleep(1.0)
        self._configure_adapter_if_needed()
        time.sleep(0.3)

        self.log("Applying global routes...")
        # Patch B: only add (and only ever remove) the /1 routes owned by this program;
        # if setup fails after the routes were added, roll them back immediately.
        try:
            self._add_owned_routes()
            with socket.create_connection((self.phone_ip, self.phone_port), timeout=5):
                pass
        except Exception:
            self._clean_routes()
            raise
        self.log("Global connection is active.")

        # Start health monitors
        self._monitoring_active = True
        self._connection_healthy = True
        # v1.5.34: failover state resets on every fresh share session.
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        self._lane_b_since = 0.0
        self._lane_a_recover_strikes = 0
        threading.Thread(target=self._monitor_process, daemon=True).start()
        threading.Thread(target=self._monitor_health, daemon=True).start()
        threading.Thread(target=self._monitor_lane_failover, daemon=True).start()
        # v1.4.6: link warmer (re-added standalone from the reverted v1.5.0).
        # Regular light traffic keeps the Wi-Fi Direct link out of power-save,
        # which is the root cause of the "dial 192.168.49.1:8080 i/o timeout"
        # storms; and sustained warmer failures trigger an EARLY rebuild check
        # so a genuine drop reconnects in seconds instead of tens of seconds.
        threading.Thread(target=self._link_keepalive_loop, daemon=True).start()
        # v1.4.93: hold the system awake for the whole share session — idle
        # sleep / Modern Standby suspends tun2socks and the Wi-Fi Direct link
        # ("a process mounted too long gets killed by the Windows power policy"). Scoped thread owns the
        # per-thread execution state and always clears it on stop.
        threading.Thread(target=self._keep_awake_loop, daemon=True).start()
        # v1.5.4 (P0): sleep/resume self-healing. Event-driven: the OS
        # callback only sets _resume_event; this thread verifies the link
        # (grace window → process alive → SOCKS greeting) and rebuilds ONLY
        # if genuinely broken. Even when the OS registration fails, this
        # thread just idles and the existing health monitor still covers it.
        self._resume_event.clear()
        self._resume_reg = _register_resume_notification(self._resume_event.set)
        if self._resume_reg:
            self.log("Registered system wake notification (verify the link after wake; rebuild only if actually broken).")
        threading.Thread(target=self._monitor_resume, daemon=True).start()

    def _monitor_resume(self):
        """v1.5.4 (P0): post-wake verification — never rebuild on wake alone.

        Wi-Fi Direct needs seconds to re-associate after the system resumes,
        so we probe at 2/5/10s (RESUME_GRACE_DELAYS_S). Outcomes:
        - process alive + SOCKS5 greeting reachable → tunnel survived, return
          to the normal monitor loops untouched (no rebuild, no adapter churn).
        - process exited while asleep → log and hand over to the bounded
          _restart_tun2socks sequence (the crash watchdog may race us; its
          _restarting mutex makes the second call a no-op).
        - still unreachable after the full window → bounded rebuild; if the
          phone itself is still away, the precheck fails and _restart_tun2socks
          retries with its own backoff — no repeated adapter deletion.
        """
        while self._monitoring_active:
            if not self._resume_event.wait(timeout=2.0):
                continue
            self._resume_event.clear()
            if not self._monitoring_active or self._restarting:
                continue
            self.log("System wake detected; waiting for Wi-Fi Direct recovery (2/5/10s windows)...")
            recovered = False
            for delay in RESUME_GRACE_DELAYS_S:
                time.sleep(delay)
                if not self._monitoring_active or self._restarting:
                    return
                if self.process and self.process.poll() is not None:
                    break  # process died during sleep — go rebuild below
                if self._probe_ok(timeout=4.0):
                    recovered = True
                    break
            if not self._monitoring_active or self._restarting:
                continue
            if recovered:
                self.log("Link verified after wake; no rebuild needed.")
            else:
                self.log("Link not recovered after wake; entering bounded rebuild.")
                self._restart_tun2socks()

    def _keep_awake_loop(self):
        """v1.4.93: ES_SYSTEM_REQUIRED for the life of the tunnel.

        SetThreadExecutionState is per-thread, so a dedicated long-lived
        thread holds it and refreshes periodically; the finally block clears
        the state no matter how the loop exits (stop, error, shutdown).
        Display timeout / CPU throttling policy is deliberately NOT touched.
        """
        if not _set_system_awake(True):
            return
        try:
            self.log("Prevented the system from auto-sleeping during sharing (screen/performance policies unaffected).")
            while self._monitoring_active:
                time.sleep(30)
                if self._monitoring_active:
                    _set_system_awake(True)  # periodic refresh, best-effort
        finally:
            _set_system_awake(False)
            self.log("System sleep policy restored.")

    def _link_keepalive_loop(self):
        """v1.4.61 — link warmer, adaptive cadence, domestic-traffic only.

        - Probe = minimal SOCKS5 greeting to the phone (handshake reserved), nothing else.
        - Cadence adapts: 30s while healthy, 15s once any failure is seen
          (stability-adaptive), back to 30s on recovery.
        - Every DOMESTIC_CHECK_EVERY_PROBES healthy probes, issue one HTTP HEAD
          to a domestic site (Baidu/Sogou/360 rotation) through the tunnel — real
          end-to-end validation with zero cross-border traffic. Failure there is logged
          silently and never triggers a rebuild by itself.
        - v1.4.92: after LINK_KEEPALIVE_FAST_STRIKES consecutive SOCKS misses
          the warmer ONLY logs and keeps probing — it never rebuilds the
          tunnel itself (power-save storms self-heal; rebuilds belong to the
          health monitor's long-probe gate).
        """
        failures = 0
        probes = 0
        domestic_idx = 0
        # v1.4.8: monotonic pacing — each probe fires at (previous due + interval)
        # on time.monotonic(), so the effective 30s/15s cadence never drifts by
        # the probe's own runtime and is immune to system clock jumps. If a
        # probe overruns its slot the next one fires immediately to catch up
        # (capped at one per loop, never a burst).
        next_due = time.monotonic()
        while self._monitoring_active:
            interval = (
                LINK_KEEPALIVE_INTERVAL_OK_S if failures == 0
                else LINK_KEEPALIVE_INTERVAL_DEGRADED_S
            )
            next_due += interval
            time.sleep(max(0.0, next_due - time.monotonic()))
            if not self._monitoring_active:
                break

            ok = False
            try:
                # v1.4.92: probe timeout 3s → 5s. Wi-Fi Direct power-save wake
                # routinely takes 3-8s; a 3s timeout counted normal link wake
                # latency as strikes and fed the (now removed) rebuild trigger.
                with socket.create_connection(
                    (self.phone_ip, self.phone_port), timeout=5
                ) as probe:
                    probe.sendall(b"\x05\x01\x00")
                    resp = probe.recv(2)
                ok = len(resp) == 2 and resp[0] == 0x05
            except Exception:
                ok = False
            # A slot missed by more than one interval (system stall) is not
            # allowed to queue up bursts — re-anchor to now.
            if time.monotonic() > next_due + interval:
                next_due = time.monotonic()

            probes += 1
            if ok:
                was_degraded = failures > 0
                failures = 0
                # Domestic site rotation (Baidu/Sogou/360): real end-to-end validation, all in-country traffic.
                if not was_degraded and probes % DOMESTIC_CHECK_EVERY_PROBES == 0:
                    self._domestic_site_touch(domestic_idx)
                    domestic_idx += 1
                continue

            failures += 1
            # v1.4.92: the warmer NO LONGER rebuilds the tunnel. Wi-Fi Direct
            # power-save storms self-heal within seconds ("Proxy connection recovered");
            # a full rebuild here tore down a working tunnel ("do not auto-restart
            # a working connection") just because the air link dozed off for ~45s. Log
            # instead and keep probing at the degraded cadence; the health
            # monitor remains the ONLY rebuild decision-maker (with its own
            # long-probe gate).
            if failures == 1:
                self.log("Link jitter detected (usually Wi-Fi Direct power-save sleep); keep-alive probing intensified...")
            elif failures % LINK_KEEPALIVE_FAST_STRIKES == 0:
                self.log(
                    f"Link unreachable for about {failures * LINK_KEEPALIVE_INTERVAL_DEGRADED_S}s,"
                    "tunnel kept (no rebuild); waiting for link self-healing or the health-check decision..."
                )

    def _domestic_site_touch(self, idx: int):
        """One silent HTTP HEAD to a domestic site through the tunnel.

        Rotates Baidu -> Sogou -> 360. Any failure is swallowed (site hiccups must
        never tear down a working tunnel); success simply keeps the path warm
        and confirms end-to-end connectivity with in-country traffic only.
        """
        host = DOMESTIC_CHECK_HOSTS[idx % len(DOMESTIC_CHECK_HOSTS)]
        try:
            conn = http.client.HTTPConnection(host, 80, timeout=6)
            conn.request("HEAD", "/", headers={"User-Agent": "Mozilla/5.0", "Connection": "close"})
            conn.getresponse().read()
            conn.close()
        except Exception:
            pass

    def is_healthy(self):
        return bool(self._monitoring_active and self._connection_healthy and
                    self.process and self.process.poll() is None)

    def _notify_tunnel_lost(self):
        if self.on_tunnel_lost:
            self.on_tunnel_lost()

    def stop(self):
        self._notify_tunnel_lost()
        self._monitoring_active = False
        self._connection_healthy = False
        # v1.5.4 (P0): kill any pending resume handling and unregister the OS
        # notification — a queued wake callback must never restart tun2socks
        # after the user stopped sharing. getattr-guarded: tests may build
        # instances via object.__new__ without __init__ state.
        resume_event = getattr(self, "_resume_event", None)
        if resume_event is not None:
            resume_event.set()  # wake the monitor thread so it exits promptly
        _unregister_resume_notification(getattr(self, "_resume_reg", None))
        self._resume_reg = None
        self._crash_times = []
        # v1.5.34: reset failover state — the next session starts on lane A.
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        self._lane_b_since = 0.0
        self._lane_a_recover_strikes = 0
        self.log("Restoring routes...")
        self._clean_routes()

        if self.process:
            self.log("Stopping connection processes...")
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception:
                pass
            # v1.4.63: belt-and-suspenders — reap the whole process tree.
            # This is what guarantees "exit means exit": a graceful terminate
            # that silently fails must never leave the tun2socks child (and
            # with it the VPN tunnel) running in the background.
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
            self.process = None
            
        subprocess.run(["netsh", "interface", "set", "interface", "LinkHelm", "disable"], capture_output=True)
        # v1.4.91: disconnect = full reset. terminate/taskkill only handles child
        # processes "we started ourselves"; a crash-restart loop can leave orphan tun2socks
        # (occupying the TUN and routes for the next connection, showing as "occupied,
        # cannot connect"). Here we sweep unconditionally and confirm death, so the
        # SOCKS5 tunnel is fully cut when the disconnect button returns.
        _kill_tun2socks_all(log_fn=self.log)
        self.log("Connection stopped.")

    def _read_tun2socks_output(self):
        """Read and log tun2socks stdout/stderr in background threads."""
        def _annotate(msg):
            # beta1.0.3: attach the corresponding cause instead of letting
            # every failure read as a bare "rejected by the remote host".
            low = msg.lower()
            if "forcibly closed by the remote host" in low or \
                    "connection reset by peer" in low:
                return msg + "  " + tr(
                    "(hint: the phone closed the session — usual causes: "
                    "session quota / PROTECT admission on the host, "
                    "credentials changed in Settings, or the share service restarting)",
                    "（提示：主端关闭了会话——常见原因：主端会话配额/高压保护准入、"
                    "设置中修改了凭据、或分享服务正在重启）")
            if "authentication failed" in low or ("auth" in low and "fail" in low):
                return msg + "  " + tr(
                    "(hint: SOCKS5 credentials mismatch — re-check them in Settings on both ends)",
                    "（提示：SOCKS5 凭据不匹配——请在两端设置中重新核对）")
            if "no such host" in low or "i/o timeout" in low or \
                    "network is unreachable" in low:
                return msg + "  " + tr(
                    "(hint: the phone is unreachable over Wi-Fi Direct — check the hotspot link)",
                    "（提示：经 Wi-Fi Direct 无法连到手机——请检查热点连接）")
            return msg

        def _reader(stream, label):
            try:
                for line in iter(stream.readline, b''):
                    if not line:
                        break
                    msg = line.decode('utf-8', errors='replace').strip()
                    if msg:
                        self.log(f"[tun2socks:{label}] {_annotate(msg)}")
            except Exception:
                pass

        if self.process and self.process.stdout:
            threading.Thread(target=_reader, args=(self.process.stdout, 'out'), daemon=True).start()
        if self.process and self.process.stderr:
            threading.Thread(target=_reader, args=(self.process.stderr, 'err'), daemon=True).start()

    def _monitor_process(self):
        # v1.5.4 (P0): fast crash watchdog. Only OUR owned child process exit
        # triggers recovery (Popen.poll on self.process — never name-based
        # sweeps here); exit is noticed in ~1s instead of 5s. Rebuilds reuse
        # the existing _restart_tun2socks bounded sequence (its _restarting
        # flag is the single mutex), and repeated crashes trip FAILED_COOLDOWN
        # instead of looping forever.
        while self._monitoring_active:
            time.sleep(CRASH_WATCHDOG_POLL_S)
            if not self._monitoring_active:
                break
            if self._restarting or not self.process:
                continue
            exit_code = self.process.poll()
            if exit_code is None:
                continue
            pid = self.process.pid
            now = time.time()
            self._crash_times = [
                t for t in self._crash_times
                if now - t < CRASH_COOLDOWN_WINDOW_S
            ]
            self._crash_times.append(now)
            if len(self._crash_times) >= CRASH_COOLDOWN_MAX:
                self._monitoring_active = False
                self._connection_healthy = False
                try:
                    self._notify_tunnel_lost()
                except Exception:
                    pass
                self.log(
                    f"Error: tun2socks (PID {pid}, exit code {exit_code}) crashed "
                    f"within {CRASH_COOLDOWN_WINDOW_S // 60} minute(s) "
                    f"{CRASH_COOLDOWN_MAX} times; stopping auto-restart (FAILED_COOLDOWN),"
                    "check the log, then reconnect manually."
                )
                return
            self.log(
                f"Watchdog: tun2socks process exited (PID {pid}, exit code {exit_code}),"
                "auto-restarting..."
            )
            self._restart_tun2socks()
            return

    def _probe_socks_port(self, port, timeout=2.0):
        """v1.5.36: lane liveness = L1 TCP connect + L2 SOCKS5 greeting.

        A greeting answer already proves the SOCKS5 server is actually alive
        — the original "hollow handshake" was a BARE TCP connect, which a dead proxy
        never answers. Egress verification moved to _socks_relay_diag:
        logged for diagnosis, never gates health.
        v1.5.47: the phone enforces userpass auth — offer [no-auth, userpass]
        and complete the subnegotiation when 0x02 is selected. Credentials
        wrong here means the config drifted → probe fails (health monitor
        handles it), never a silent false-healthy.
        """
        try:
            with socket.create_connection((self.phone_ip, port), timeout=timeout) as s:
                s.settimeout(timeout)
                return socks5_greet(
                    s, self.socks_user, self.socks_pass, timeout=timeout
                ) == "SOCKS5_OK"
        except Exception:
            return False

    def _socks_relay_diag(self, port, timeout=3.0):
        """v1.5.36: L3 CONNECT-relay diagnostic — LOG-ONLY, never gates health.

        Returns a short status: "ok" (REP=0x00), "refused(REP=0xNN)",
        "no-socks5", "egress-silent" or "unreachable". Used in failure paths
        to tell "phone proxy dead" apart from "phone has no usable egress"
        (e.g. carrier filters TCP/53) in the field log.
        """
        try:
            with socket.create_connection((self.phone_ip, port), timeout=timeout) as s:
                s.settimeout(timeout)
                if socks5_greet(
                    s, self.socks_user, self.socks_pass, timeout=timeout
                ) != "SOCKS5_OK":
                    return "no-socks5"
                host, sport = LANE_PROBE_TARGET
                s.sendall(
                    b"\x05\x01\x00\x01"
                    + socket.inet_aton(host)
                    + struct.pack(">H", sport)
                )
                resp = s.recv(10)
                if len(resp) >= 2 and resp[0] == 5:
                    if resp[1] == 0:
                        return "ok"
                    # beta1.0.3: surface the actual REP meaning instead of a
                    # bare hex code — the field log should say WHY the phone
                    # refused, not just "rejected".
                    return (f"refused(REP=0x{resp[1]:02x} "
                            f"{socks5_rep_meaning(resp[1])})")
                return "egress-silent"
        except socket.timeout:
            return "egress-silent"
        except Exception:
            return "unreachable"

    def _secondary_lane_available(self, timeout=3.0):
        """v1.5.2: probe the phone's standby lane (8082) with a SOCKS5 greeting.

        Returns True only when a real SOCKS5 server answers — an old APK
        (no secondary listener) or a firewall RST both land here as False.
        """
        return self._probe_socks_port(SECONDARY_LANE_PORT, timeout=timeout)

    def _tun2socks_proxy_url(self):
        """The -proxy URL tun2socks should dial for the current mode.

        v1.5.47: credentials embedded (RFC 1929 userpass). Alphanumeric-only
        credentials need no URL escaping; quote() is applied anyway for safety.
        """
        user = urllib.parse.quote(self.socks_user, safe="")
        pwd = urllib.parse.quote(self.socks_pass, safe="")
        if self._use_lane_b:
            # v1.5.34: failover active — tunnel moved to the standby lane.
            return f"socks5://{user}:{pwd}@{self.phone_ip}:{SECONDARY_LANE_PORT}"
        return f"socks5://{user}:{pwd}@{self.phone_ip}:{self.phone_port}"

    def _probe_ok(self, timeout=6.0):
        """v1.4.2: single lightweight reachability probe (no state changes).
        v1.4.92: default timeout 4s → 6s — Wi-Fi Direct power-save wake takes
        3-8s, and a too-short timeout manufactured false strikes.
        """
        # v1.5.36: probe whichever lane the tunnel is CURRENTLY dialling, at
        # greeting level (the server is genuinely alive — the original
        # the "hollow handshake" was bare TCP). While failed over to :8082, lane A being
        # alive must not mask a standby-lane outage (and vice versa).
        active_port = SECONDARY_LANE_PORT if self._use_lane_b else self.phone_port
        return self._probe_socks_port(active_port, timeout=timeout)

    def _monitor_health(self):
        # v1.4.2: 10s interval + confirm probe + 3-strike threshold.
        # A Wi-Fi Direct power-save blip lasts a few seconds; the old logic
        # (15s interval, single probe, restart after 2 strikes ≈ 30s) tore down
        # and rebuilt the whole tunnel far too eagerly — that was the reported
        # "occasional disconnects and stutter". Now: probe -> confirm 3s later -> count strike;
        # sharing stops at 2 strikes, tunnel rebuild only at 3.
        # v1.4.92: rebuild additionally requires a LONG probe (10s) to fail —
        # the final gate. Link wake latency up to ~8s no longer counts as an
        # outage, so a connected tunnel is not rebuilt while it would recover
        # on its own ("do not auto-restart a working connection").
        fail_count = 0
        while self._monitoring_active:
            time.sleep(10)
            if not self._monitoring_active:
                break
            if self._probe_ok():
                if fail_count > 0:
                    self.log("Proxy connection recovered")
                fail_count = 0
                self._connection_healthy = True
                continue
            # confirm probe: only a real outage fails twice in a row
            time.sleep(3)
            if self._probe_ok():
                self.log("Proxy connection recovered")
                fail_count = 0
                self._connection_healthy = True
                continue
            fail_count += 1
            if fail_count == 1:
                self._connection_healthy = False
                self.log("Proxy health check failed (1/3); waiting for self-healing...")
            elif fail_count == 2:
                self._connection_healthy = False
                self.log("Proxy health check failed (2/3); stopping sharing")
                try:
                    self._notify_tunnel_lost()
                except Exception as error:
                    self.log(f"Failed to stop sharing: {error}")
            elif fail_count >= 3:
                self._connection_healthy = False
                # v1.5.36: log-only egress diagnostic before the long probe —
                # explains in the field log whether the failure is the tunnel
                # (proxy dead) or the phone's own egress (e.g. carrier filters
                # TCP/53) without gating anything on it.
                try:
                    active_port = (
                        SECONDARY_LANE_PORT if self._use_lane_b else self.phone_port
                    )
                    self.log(
                        f"Egress relay diagnostics (logged only, not used for decisions):"
                        f"{self._socks_relay_diag(active_port)}"
                    )
                except Exception:
                    pass
                self.log("Proxy unreachable; running a final long probe (10s) to confirm a real drop...")
                if self._probe_ok(timeout=10.0):
                    self.log("Long probe passed: link self-healed, no tunnel rebuild")
                    fail_count = 0
                    self._connection_healthy = True
                    continue
                self.log("Long probe still failed; tunnel confirmed down, restarting...")
                self._restart_tun2socks()
                return

    def _monitor_lane_failover(self):
        """v1.5.34: standby-lane failover WITHOUT the lane router.

        Field data (v1.5.31-1.5.33) showed the full dual-lane router caused
        more failures than it fixed, so the default data plane is again
        tun2socks → phone:8080 directly. This monitor only WATCHES the lanes
        with greeting-level SOCKS5 liveness probes (v1.5.36: connect →
        negotiate; CONNECT-egress is the PHONE's network and must never
        gate switching — see _probe_socks_port):

        - Primary :8080 fails 3 consecutive probes (~3s each, 5s apart) AND
          the standby :8082 answers → rebuild tunnel on B.
        - While on B (v1.5.35 R3): switch back only after the standby has
          been dwelt on ≥ LANE_B_MIN_DWELL_S AND the primary passes
          LANE_A_RECOVER_STRIKES consecutive probes — no more "responds -> switch
          back -> instantly drops again" flapping. Exception: if B dies while A is back,
          switch immediately (never stay on a dead lane).
        - If BOTH lanes fail, leave it to the existing health monitor
          (which probes the ACTIVE lane) to handle the full outage.

        No session spreading, no local UDP termination — the proven 1.4.93
        behaviour plus an escape door.
        """
        while self._monitoring_active:
            time.sleep(5)
            if not self._monitoring_active or self._restarting:
                continue
            if not self._use_lane_b:
                # v1.5.35: 3s window — under heavy concurrent load (many
                # pages + game UDP flows) a 2s probe could false-timeout and
                # trigger a needless switch/rebuild.
                if self._probe_socks_port(self.phone_port, timeout=3.0):
                    if self._lane_a_fail_strikes:
                        self.log("Primary channel :8080 probe recovered.")
                    self._lane_a_fail_strikes = 0
                    continue
                self._lane_a_fail_strikes += 1
                # v1.5.46 (beta1.0.2): 3 strikes under load were too twitchy —
                # a busy phone (heavy pages, file transfer) can stall three
                # 3s probes in a row and trigger a needless tunnel rebuild
                # ("frequent disconnect/reconnect" reports). Now 5 strikes,
                # and before any switch a 6s confirm probe must ALSO fail.
                if self._lane_a_fail_strikes < 5:
                    self.log(
                        f"Primary :8080 probe timed out ({self._lane_a_fail_strikes}/5); observing..."
                    )
                    continue
                if self._probe_socks_port(self.phone_port, timeout=6.0):
                    self.log(
                        "Primary :8080 confirm probe (6s) passed; the earlier timeouts were transient, no lane switch."
                    )
                    self._lane_a_fail_strikes = 0
                    continue
                if not self._probe_socks_port(SECONDARY_LANE_PORT, timeout=3.0):
                    self.log(
                        "Primary :8080 unreachable, but backup :8082 is also unavailable;"
                        "keeping current state (overall disconnect handled by the health check)."
                    )
                    continue
                # v1.5.36: log-only egress diagnostic — shows whether the
                # primary failure is the proxy or the phone's own uplink.
                self.log(
                    f"Primary :8080 egress relay diagnostics (logged only):"
                    f"{self._socks_relay_diag(self.phone_port)}"
                )
                self.log("Primary channel :8080 unreachable; switching to backup channel :8082 ...")
                self._use_lane_b = True
                self._lane_b_since = time.time()
                self._lane_a_recover_strikes = 0
                self._restart_tun2socks()
            else:
                # v1.5.35 (R3): switch-back HYSTERESIS. A single probe
                # success no longer yanks the tunnel back to A (the field
                # symptom was "switch back the moment it responds -> immediately drop again"). A must pass
                # LANE_A_RECOVER_STRIKES consecutive real-handshake probes
                # AND B must have been dwelt on for LANE_B_MIN_DWELL_S.
                a_ok = self._probe_socks_port(self.phone_port, timeout=2.0)
                b_ok = self._probe_socks_port(SECONDARY_LANE_PORT, timeout=2.0)
                if a_ok and not b_ok:
                    # Standby died while primary is back — switch now; the
                    # dwell gate must not keep us on a dead lane.
                    self.log("Backup channel :8082 unavailable and primary :8080 recovered; switching back to primary ...")
                    self._use_lane_b = False
                    self._lane_a_fail_strikes = 0
                    self._lane_a_recover_strikes = 0
                    self._restart_tun2socks()
                elif a_ok:
                    self._lane_a_recover_strikes += 1
                    dwell = time.time() - self._lane_b_since
                    if (self._lane_a_recover_strikes >= LANE_A_RECOVER_STRIKES
                            and dwell >= LANE_B_MIN_DWELL_S):
                        self.log(
                            f"Primary :8080 passed {self._lane_a_recover_strikes} consecutive real handshakes"
                            f"(backup dwell {dwell:.0f}s); switching back to primary ..."
                        )
                        self._use_lane_b = False
                        self._lane_a_fail_strikes = 0
                        self._lane_a_recover_strikes = 0
                        self._restart_tun2socks()
                else:
                    self._lane_a_recover_strikes = 0
                    if not b_ok:
                        self.log("Backup channel :8082 probe timed out; awaiting health-check decision.")

    def _restart_tun2socks(self):
        # v1.4.2: rebuild with up to 3 backoff attempts (3s/6s/10s) instead of
        # giving up after one failure — a transient outage now self-heals and
        # the VPN no longer dies waiting for the user to press Connect again.
        if self._restarting or not self._monitoring_active:
            return
        self._restarting = True
        self._connection_healthy = False
        try:
            last_error = None
            for attempt, backoff in ((1, 0), (2, 3), (3, 6), (4, 10)):
                try:
                    if attempt > 1:
                        if not self._monitoring_active:
                            return
                        self.log(f"Tunnel restart attempt {attempt}/4 (waiting {backoff}s first)...")
                        time.sleep(backoff)
                        if not self._monitoring_active:
                            return
                    self._rebuild_tunnel()
                    self._connection_healthy = True
                    self.log("Tunnel restart succeeded")
                    threading.Thread(target=self._monitor_process, daemon=True).start()
                    threading.Thread(target=self._monitor_health, daemon=True).start()
                    # v1.4.6: the old keepalive loop returned after triggering
                    # the rebuild — start a fresh one for the new tunnel.
                    threading.Thread(target=self._link_keepalive_loop, daemon=True).start()
                    return
                except Exception as error:
                    last_error = error
                    self.log(f"Tunnel restart attempt {attempt}/4 failed: {error}")
            self._monitoring_active = False
            self._connection_healthy = False
            self.log(f"Tunnel restart failed (retried 4 times): {last_error}")
        finally:
            self._restarting = False

    def _rebuild_tunnel(self):
        # Round-2 fix: do not rebuild the tunnel against a dead phone endpoint.
        # v1.4.2: precheck with retries; raise → caller retries with backoff.
        if self._use_lane_b:
            # v1.5.34: failed-over rebuild must precheck the lane it will
            # actually dial, not lane A.
            precheck = check_socks5_with_retry(
                self.phone_ip, SECONDARY_LANE_PORT, log_fn=self.log,
                socks_user=self.socks_user, socks_pass=self.socks_pass
            )
        else:
            precheck = check_socks5_with_retry(
                self.phone_ip, self.phone_port, log_fn=self.log,
                socks_user=self.socks_user, socks_pass=self.socks_pass
            )
        if precheck != "SOCKS5_OK":
            diagnosis = diagnose_tcp_failure(self.phone_ip, precheck)
            if diagnosis:
                self.log(f"Diagnostics: {diagnosis}")
            raise RuntimeError(f"Phone proxy precheck failed on restart: {precheck}")
        self._notify_tunnel_lost()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

        # v1.4.91: sweep orphans before relaunching. Repeated crash-restarts
        # can accumulate tun2socks processes that are NOT our current child;
        # a new instance started while one of those holds the TUN adapter is
        # exactly the "reconnect fails (occupied)" symptom.
        _kill_tun2socks_all(log_fn=self.log)

        self._clean_routes()
        if not self._monitoring_active:
            return
        # v1.4.4: re-verify (and self-heal from bundled copies if needed) before
        # every relaunch — "verify before each use", not just at first start.
        t2s_exe = self._check_dependencies()
        cmd = tun2socks_cmd(t2s_exe, self._tun2socks_proxy_url())
        cflags = 0x08000000 if IS_WINDOWS else 0
        self.process = subprocess.Popen(
            cmd, cwd=BIN_DIR, creationflags=cflags,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._read_tun2socks_output()
        self.log("Waiting for the adapter to be ready...")
        for _ in range(15):
            if self.process.poll() is not None:
                raise RuntimeError("tun2socks exited while restarting.")
            # v1.3.2: _run_decoded — same GBK fix as the initial start path.
            _, out, _ = _run_decoded(
                ["netsh", "interface", "ipv4", "show", "interfaces"], timeout=15
            )
            if "LinkHelm" in out:
                break
            time.sleep(1)
        else:
            raise RuntimeError("VPN adapter did not reappear.")

        # v1.5.41 (P0): idempotent config — reuse what is already correct.
        self._configure_adapter_if_needed()
        self._add_owned_routes()
        if not self._probe_ok():
            raise RuntimeError("proxy unreachable after tunnel rebuild")

class HttpSocksBridge:
    def __init__(self, local_port, socks_host, socks_port, log_fn=None):
        self.local_port = local_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.log = log_fn or print
        self._running = False
        self._server = None

    def start(self):
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except:
                pass

    def _serve(self):
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", self.local_port))
            self._server.listen(100)
            self._server.settimeout(1.0)
            self.log(f"Bridge listening on 127.0.0.1:{self.local_port}")
        except Exception as e:
            self.log(f"Bridge start failed: {e}")
            return

        while self._running:
            try:
                conn, addr = self._server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle(self, client):
        try:
            client.settimeout(10)
            data = b""
            while b"\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk: return
                data += chunk

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
            parts = first_line.split()
            if len(parts) < 3: return
            method, target = parts[0], parts[1]

            if method.upper() == "CONNECT":
                host, port = target.rsplit(":", 1)
                port = int(port)
            else:
                from urllib.parse import urlparse
                parsed = urlparse(target)
                host, port = parsed.hostname or "", parsed.port or 80

            relay = self._socks5_connect(host, port)
            if relay is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            if method.upper() == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._pipe(client, relay)
            else:
                relay.sendall(data)
                self._pipe(client, relay)
        except Exception as e:
            self.log(f"Bridge handler error: {e}")
        finally:
            try: client.close()
            except: pass

    def _socks5_connect(self, host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((self.socks_host, self.socks_port))
            s.sendall(b"\x05\x01\x00")
            if s.recv(2)[1] != 0x00: return None
            
            # Request connection
            try:
                # IPv4
                s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port))
            except:
                # Domain name
                host_bytes = host.encode()
                s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack(">H", port))
            
            res = s.recv(10)
            if len(res) < 2 or res[1] != 0x00: return None
            return s
        except Exception:
            return None

    def _pipe(self, a, b):
        a.settimeout(None)
        b.settimeout(None)
        sockets = [a, b]
        try:
            while True:
                r, _, e = select.select(sockets, [], sockets, 30)
                if e: break
                for s in r:
                    other = b if s is a else a
                    chunk = s.recv(32768)
                    if not chunk: return
                    other.sendall(chunk)
        finally:
            for s in (a, b):
                try: s.close()
                except: pass

class App:
    BG = ("#F0F2F7", "#101827")
    CARD = ("#FFFFFF", "#1B2638")
    TEXT = ("#1C222B", "#F0F5FC")
    MUTED = ("#657182", "#A9B7C9")
    SUBTLE = ("#9BA4B0", "#8291A5")
    BORDER = ("#E2E6ED", "#344359")
    INPUT_BORDER = ("#D0D6E0", "#46566B")
    INPUT_BG = ("#FBFCFD", "#202D40")
    STATUS_BG = ("#F3F5F8", "#263448")
    ACCENT = "#0067C0"

    def __init__(self, root):
        self.root = root
        self.root.title("LinkHelm Desktop beta1.0.3 | Windows")
        self.root.geometry(f"460x{min(770, max(620, root.winfo_screenheight() - 90))}")
        self.root.minsize(420, 590)
        self.root.configure(fg_color=self.BG)

        self.tun_mgr = None
        self.ics_mgr = IcsManager(self.log)
        self._bridge_lock = threading.RLock()
        self._bridge_busy = False
        self._ethernet_choices = {}
        self._activity_open = False
        self._advanced_open = False

        self.main = ctk.CTkScrollableFrame(root, fg_color=self.BG, corner_radius=0)
        self.main.pack(fill="both", expand=True)
        self.content = ctk.CTkFrame(self.main, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=14, pady=(13, 20))

        header = ctk.CTkFrame(self.content, fg_color="transparent")
        header.pack(fill="x", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text=tr("Connect & Share", "连接与共享"), font=("Segoe UI", 21, "bold"),
            text_color=self.TEXT, anchor="w", height=28
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, text=tr("WINDOWS CLIENT", "WINDOWS 客户端"), font=("Segoe UI", 9, "bold"),
            fg_color=("#E3E7F0", "#2A3B51"), text_color=self.MUTED,
            corner_radius=11, width=108, height=23
        ).grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            header, text=tr("Android phone   →   Windows client   →   Target devices", "安卓手机   →   Windows 连接   →   目标设备"),
            font=("Segoe UI", 11), text_color=self.MUTED, anchor="w", height=20
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        # v1.5.44: in-window Settings entry (the tray menu has one too).
        ctk.CTkButton(
            header, text=tr("⚙ Settings", "⚙ 设置"), width=76, height=26, corner_radius=13,
            font=("Segoe UI", 11), fg_color=self.CARD, hover_color=self.BORDER,
            text_color=self.TEXT, border_width=1, border_color=self.BORDER,
            command=self.open_settings
        ).grid(row=1, column=1, sticky="e", pady=(2, 0))

        vpn_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        vpn_card.pack(fill="x", pady=(0, 12))
        vpn_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            vpn_card, text=tr("01  INBOUND", "01  入站连接"), font=("Segoe UI", 9, "bold"),
            text_color=self.ACCENT, anchor="w", height=15
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        ctk.CTkLabel(
            vpn_card, text=tr("Connect to your phone", "连接到手机"), font=("Segoe UI", 16, "bold"),
            text_color=self.TEXT, anchor="w", height=24
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(1, 0))
        ctk.CTkLabel(
            vpn_card, text=tr("Enable Share Mode on the Android device and join its Wi-Fi Direct network.", "在安卓端开启共享模式，并加入其 Wi-Fi Direct 网络。"),
            font=("Segoe UI", 10), text_color=self.MUTED, anchor="w", height=19
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 11))

        fields = ctk.CTkFrame(vpn_card, fg_color="transparent")
        fields.grid(row=3, column=0, sticky="ew", padx=16)
        fields.grid_columnconfigure(0, weight=2)
        fields.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(fields, text=tr("Phone IP Address", "手机 IP 地址"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=18).grid(
            row=0, column=0, sticky="w", pady=(0, 3)
        )
        ctk.CTkLabel(fields, text=tr("SOCKS5 Port", "SOCKS5 端口"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=18).grid(
            row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 3)
        )
        self.phone_ip = ctk.CTkEntry(
            fields, height=33, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 11)
        )
        self.phone_ip.insert(0, "192.168.49.1")
        self.phone_ip.grid(row=1, column=0, sticky="ew")
        self.phone_port = ctk.CTkEntry(
            fields, height=33, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 11)
        )
        self.phone_port.insert(0, "8080")
        self.phone_port.grid(row=1, column=1, sticky="ew", padx=(10, 0))

        # v1.5.47: SOCKS5 credentials — pre-filled with the same fixed initial
        # pair as the APK (never randomized), editable before connecting.
        _creds = load_app_settings()
        ctk.CTkLabel(fields, text=tr("SOCKS5 User", "SOCKS5 账号"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=18).grid(
            row=2, column=0, sticky="w", pady=(8, 3)
        )
        ctk.CTkLabel(fields, text=tr("SOCKS5 Password", "SOCKS5 密码"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=18).grid(
            row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 3)
        )
        self.phone_user = ctk.CTkEntry(
            fields, height=33, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 11)
        )
        self.phone_user.insert(0, _creds.get("socksUser") or DEFAULT_PHONE_USER)
        self.phone_user.grid(row=3, column=0, sticky="ew")
        self.phone_pass = ctk.CTkEntry(
            fields, height=33, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 11)
        )
        self.phone_pass.insert(0, _creds.get("socksPass") or DEFAULT_PHONE_PASS)
        self.phone_pass.grid(row=3, column=1, sticky="ew", padx=(10, 0))

        self.advanced_button = ctk.CTkButton(
            vpn_card, text=tr("Advanced  ⌄", "高级设置  ⌄"), width=133, height=25,
            fg_color="transparent", hover_color=self.STATUS_BG,
            text_color=self.ACCENT, font=("Segoe UI", 10, "bold"),
            anchor="w", command=self._toggle_advanced
        )
        self.advanced_button.grid(row=4, column=0, sticky="w", padx=12, pady=(5, 0))
        self.advanced_frame = ctk.CTkFrame(vpn_card, fg_color=self.STATUS_BG, corner_radius=7)
        self.advanced_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.advanced_frame, text=tr("Local download bridge port", "本地下载桥接端口"),
            text_color=self.TEXT, font=("Segoe UI", 10, "bold"), anchor="w", height=18
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.local_port = ctk.CTkEntry(
            self.advanced_frame, height=32, corner_radius=7,
            fg_color=self.INPUT_BG, border_color=self.INPUT_BORDER
        )
        # v1.3: use 18080 by default to avoid confusion with the phone SOCKS port (8080).
        self.local_port.insert(0, "18080")
        self.local_port.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 9))

        status_row = ctk.CTkFrame(vpn_card, fg_color=self.STATUS_BG, corner_radius=7)
        status_row.grid(row=6, column=0, sticky="ew", padx=16, pady=(12, 12))
        status_row.grid_columnconfigure(1, weight=1)
        self.status_dot = ctk.CTkFrame(
            status_row, width=8, height=8, corner_radius=4, fg_color="#8B95A1"
        )
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=13)
        self.status_var = ctk.StringVar(value=tr("Disconnected", "未连接"))
        self.status_label = ctk.CTkLabel(
            status_row, textvariable=self.status_var, font=("Segoe UI", 11, "bold"),
            text_color=self.TEXT, anchor="w", height=18
        )
        self.status_label.grid(row=0, column=1, sticky="sw", pady=(8, 0))
        self.status_detail = ctk.StringVar(value=tr("Enter the phone IP, then tap Connect.", "输入手机 IP 后点击「连接」。"))
        ctk.CTkLabel(
            status_row, textvariable=self.status_detail, font=("Segoe UI", 10),
            text_color=self.MUTED, anchor="w", height=17
        ).grid(row=1, column=1, sticky="nw", pady=(0, 8))
        self.progress = ctk.CTkProgressBar(vpn_card, mode="indeterminate", progress_color=self.ACCENT)
        self.progress.set(0)

        vpn_actions = ctk.CTkFrame(vpn_card, fg_color="transparent")
        vpn_actions.grid(row=8, column=0, sticky="ew", padx=16, pady=(0, 16))
        vpn_actions.grid_columnconfigure(0, weight=1)
        vpn_actions.grid_columnconfigure(1, weight=1)
        self.btn_start = ctk.CTkButton(
            vpn_actions, text=tr("▷  Connect", "▷  连接"), height=35, corner_radius=7,
            fg_color=self.ACCENT, hover_color="#005AAB",
            font=("Segoe UI", 11, "bold"), command=self.start
        )
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_stop = ctk.CTkButton(
            vpn_actions, text=tr("Disconnect", "断开连接"), height=35, corner_radius=7,
            state="disabled", fg_color=self.INPUT_BG,
            border_width=1, border_color=self.BORDER,
            text_color=self.TEXT, hover_color=self.STATUS_BG,
            font=("Segoe UI", 11), command=self.stop
        )
        self.btn_stop.grid(row=0, column=1, sticky="ew")

        self.bridge_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        self.bridge_card.pack(fill="x", pady=(0, 12))
        self.bridge_card.grid_columnconfigure(0, weight=1)
        self.bridge_eyebrow = ctk.CTkLabel(
            self.bridge_card, text=tr("02  OUTBOUND", "02  出站分发"), font=("Segoe UI", 9, "bold"),
            text_color=self.ACCENT, anchor="w", height=15
        )
        self.bridge_eyebrow.grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        self.bridge_title = ctk.CTkLabel(
            self.bridge_card, text=tr("Share to other devices", "共享给其他设备"),
            font=("Segoe UI", 16, "bold"), text_color=self.TEXT, anchor="w", height=24
        )
        self.bridge_title.grid(row=1, column=0, sticky="w", padx=16, pady=(1, 0))
        self.bridge_subtitle = ctk.CTkLabel(
            self.bridge_card, text=tr("Client devices need no proxy configuration.", "客户端设备无需任何代理设置。"),
            font=("Segoe UI", 10), text_color=self.MUTED, anchor="w", height=19
        )
        self.bridge_subtitle.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 10))
        self.bridge_mode = ctk.CTkSegmentedButton(
            self.bridge_card, values=[tr("Wi-Fi Hotspot", "Wi-Fi 热点"), tr("Ethernet", "网线直连")],
            command=self._bridge_mode_changed, height=35,
            fg_color=("#EEF1F6", "#263448"), border_width=3,
            selected_color=("#FFFFFF", "#364960"),
            selected_hover_color=("#FFFFFF", "#40556F"),
            unselected_color=("#EEF1F6", "#263448"),
            unselected_hover_color=("#E4E9F0", "#314157"),
            text_color=self.TEXT, font=("Segoe UI", 11)
        )
        self.bridge_mode.grid(row=3, column=0, sticky="ew", padx=16)
        self.bridge_mode.set(tr("Wi-Fi Hotspot", "Wi-Fi 热点"))

        self.wifi_row = ctk.CTkFrame(self.bridge_card, fg_color="transparent")
        self.wifi_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(12, 0))
        self.wifi_row.grid_columnconfigure(0, weight=2)
        self.wifi_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            self.wifi_row, text=tr("Hotspot Name (SSID)", "热点名称 (SSID)"),
            font=("Segoe UI", 10), text_color=self.MUTED, anchor="w", height=18
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))
        ctk.CTkLabel(
            self.wifi_row, text=tr("Password", "密码"), font=("Segoe UI", 10),
            text_color=self.MUTED, anchor="w", height=18
        ).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 3))
        self.hotspot_ssid_var = ctk.StringVar(value=tr("Shown after start", "启动后显示"))
        self.hotspot_password_var = ctk.StringVar(value=tr("Shown after start", "启动后显示"))
        self.hotspot_ssid = ctk.CTkEntry(
            self.wifi_row, textvariable=self.hotspot_ssid_var,
            placeholder_text=tr("Configured in Windows Settings", "在 Windows 设置中配置"), height=32,
            corner_radius=7, fg_color=self.INPUT_BG, border_color=self.INPUT_BORDER,
            font=("Segoe UI", 10)
        )
        self.hotspot_ssid.grid(row=1, column=0, sticky="ew")
        self.hotspot_password = ctk.CTkEntry(
            self.wifi_row, textvariable=self.hotspot_password_var,
            placeholder_text=tr("Windows Settings", "Windows 设置"), height=32,
            corner_radius=7, fg_color=self.INPUT_BG, border_color=self.INPUT_BORDER,
            font=("Segoe UI", 10)
        )
        self.hotspot_password.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        self.hotspot_ssid.configure(state="readonly")
        self.hotspot_password.configure(state="readonly")

        self.ethernet_row = ctk.CTkFrame(self.bridge_card, fg_color="transparent")
        self.ethernet_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(12, 0))
        self.ethernet_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.ethernet_row, text=tr("Connected Ethernet adapter", "已连接的以太网适配器"),
            font=("Segoe UI", 10), text_color=self.MUTED, anchor="w", height=18
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
        self.ethernet_choice = ctk.CTkComboBox(
            self.ethernet_row, values=[tr("Connect cable, then refresh", "先连接网线再刷新")],
            height=32, state="readonly", fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER
        )
        self.ethernet_choice.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.ethernet_choice.set(tr("Connect cable, then refresh", "先连接网线再刷新"))
        self.refresh_ethernet_button = ctk.CTkButton(
            self.ethernet_row, text=tr("Refresh", "刷新"), width=76, height=32,
            corner_radius=7, fg_color=self.STATUS_BG, text_color=self.TEXT,
            hover_color=self.BORDER, command=self._refresh_ethernet
        )
        self.refresh_ethernet_button.grid(row=1, column=1)
        self.ethernet_row.grid_remove()

        bridge_status_row = ctk.CTkFrame(
            self.bridge_card, fg_color=self.STATUS_BG, corner_radius=7
        )
        bridge_status_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(12, 12))
        bridge_status_row.grid_columnconfigure(1, weight=1)
        self.bridge_status_dot = ctk.CTkFrame(
            bridge_status_row, width=8, height=8, corner_radius=4, fg_color="#8B95A1"
        )
        self.bridge_status_dot.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=13)
        self.bridge_status_title = ctk.StringVar(value=tr("Waiting", "等待连接"))
        ctk.CTkLabel(
            bridge_status_row, textvariable=self.bridge_status_title,
            font=("Segoe UI", 11, "bold"), text_color=self.TEXT,
            anchor="w", height=18
        ).grid(row=0, column=1, sticky="sw", pady=(8, 0))
        self.bridge_status = ctk.StringVar(value=tr("Connect the phone in step 1 first.", "请先在第 1 步连接手机。"))
        self.bridge_help = ctk.CTkLabel(
            bridge_status_row, textvariable=self.bridge_status,
            wraplength=360, justify="left", anchor="w",
            font=("Segoe UI", 10), text_color=self.MUTED
        )
        self.bridge_help.grid(row=1, column=1, sticky="nw", pady=(0, 8))
        self.bridge_status.trace_add("write", self._bridge_message_changed)

        self.bridge_button = ctk.CTkButton(
            self.bridge_card, text=tr("Start Sharing", "开始共享网络"), height=35,
            corner_radius=7, state="disabled", command=self._on_share_button,
            fg_color=self.BORDER, hover_color="#005AAB",
            text_color_disabled=self.MUTED,
            font=("Segoe UI", 11, "bold")
        )
        self.bridge_button.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 16))

        # v1.4.3: one-tap UDP connectivity check (live test via the phone SOCKS5 UDP relay, no system changes)
        self.udp_button = ctk.CTkButton(
            self.content, text=tr("Check UDP Connectivity", "检测 UDP 连通性"), height=30,
            fg_color="transparent", border_width=1, border_color=self.BORDER,
            text_color=self.TEXT, hover_color=self.STATUS_BG,
            font=("Segoe UI", 11), command=self._on_udp_check
        )
        self.udp_button.pack(fill="x", pady=(0, 12))

        # v1.5.45 (beta1.0.1): file-transfer trial entry — single file <= 1 GB
        # over the Wi-Fi Direct link only (port 8765), independent of the
        # tunnel; files never touch the phone's internet exit.
        ft_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        ft_card.pack(fill="x", pady=(12, 0))
        ft_header = ctk.CTkFrame(ft_card, fg_color="transparent")
        ft_header.pack(fill="x", padx=16, pady=9)
        ctk.CTkLabel(
            ft_header, text=tr("File Transfer (trial)", "文件传输（试用版）"),
            font=("Segoe UI", 11, "bold"), text_color=self.TEXT
        ).pack(side="left")
        ctk.CTkButton(
            ft_header, text=tr("Open", "打开"), width=95, height=24,
            fg_color="transparent", hover_color=self.STATUS_BG,
            text_color=self.ACCENT, command=self._open_file_transfer_dialog
        ).pack(side="right")

        activity_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        activity_card.pack(fill="x")
        activity_header = ctk.CTkFrame(activity_card, fg_color="transparent")
        activity_header.pack(fill="x", padx=16, pady=9)
        ctk.CTkLabel(
            activity_header, text=tr("Activity Log", "活动日志"), font=("Segoe UI", 11, "bold"),
            text_color=self.TEXT
        ).pack(side="left")
        self.activity_button = ctk.CTkButton(
            activity_header, text=tr("Show Log", "显示日志"), width=95, height=24,
            fg_color="transparent", hover_color=self.STATUS_BG,
            text_color=self.ACCENT, command=self._toggle_activity
        )
        self.activity_button.pack(side="right")
        self.activity_body = ctk.CTkFrame(activity_card, fg_color="transparent")
        self.log_text = ctk.CTkTextbox(
            self.activity_body, font=("Consolas", 10), height=130, wrap="word"
        )
        self.log_text.pack(fill="x", padx=16)
        self.log_text._textbox.tag_configure("error", foreground="#F87171")
        self.log_text._textbox.tag_configure("success", foreground="#4ADE80")
        self.log_text.configure(state="disabled")
        ctk.CTkButton(
            self.activity_body, text=tr("Clear Log", "清空日志"), width=80, height=24,
            fg_color="transparent", border_width=1, border_color=self.BORDER,
            text_color=self.MUTED, command=self._clear_log
        ).pack(anchor="e", padx=16, pady=(6, 12))

        # Footer: license notice + repository link (consistent with the MIT LICENSE).
        footer = ctk.CTkFrame(self.content, fg_color="transparent")
        footer.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            footer,
            text=tr("Built for personal learning and development. Licensed under the MIT License (see LICENSE); for commercial use the author asks for prior contact (jak21117@gmail.com).", "面向个人学习与开发。按 MIT 许可证授权（见 LICENSE）；如需商用，请先联系作者 (jak21117@gmail.com)。"),
            font=("Segoe UI", 10), text_color=self.MUTED, wraplength=560, justify="center"
        ).pack()
        ctk.CTkLabel(
            footer,
            text="github.com/jak21117-maker/LinkHelm",
            font=("Segoe UI", 10), text_color="#2E9BFF", cursor="hand2"
        ).pack()
        link_label = footer.winfo_children()[-1]
        link_label.bind(
            "<Button-1>",
            lambda _e: webbrowser.open("https://github.com/jak21117-maker/LinkHelm"),
        )
        ctk.CTkLabel(
            footer,
            text="© 2026 LinkHelm Developers",
            font=("Segoe UI", 10), text_color=self.MUTED
        ).pack()

        self._update_bridge_button()
        threading.Thread(target=self._load_hotspot_info, daemon=True).start()

    def _load_hotspot_info(self):
        try:
            ssid, password = self.ics_mgr.hotspot_info()
            if ssid and password:
                self.root.after(0, lambda: self._set_hotspot_info(ssid, password))
        except (BridgeError, RuntimeError):
            pass  # Windows may not expose a Mobile Hotspot configuration yet.

    def _set_hotspot_info(self, ssid, password):
        self.hotspot_ssid_var.set(ssid)
        self.hotspot_password_var.set(password)

    def _on_share_button(self):
        if self.ics_mgr.needs_cleanup:
            self._stop_bridge()
        else:
            self._toggle_bridge()

    def _bridge_message_changed(self, *_args):
        message = self.bridge_status.get()
        if message.startswith((tr("Error: ", "错误："), tr("Cleanup error: ", "清理错误："), tr("Link lost", "连接断开"))):
            title, color = tr("Sharing needs attention", "共享需要处理"), "#E45D63"
        elif message.startswith(tr("Connect devices to ", "请将设备")):
            title, color = tr("Sharing is on", "共享已开启"), "#23B885"
        elif message.startswith((tr("Configuring", "正在配置"), tr("Stopping", "正在停止"))):
            title, color = tr("Updating sharing", "正在更新共享"), "#E59A31"
        elif message.startswith((tr("Windows will", "Windows 将"), tr("Plug in the cable", "请连接网线"))):
            title, color = tr("Ready to share", "可以开始共享"), "#8796A8"
        elif message.startswith(tr("Sharing stopped", "共享已停止")):
            title, color = tr("Sharing is off", "共享已关闭"), "#8796A8"
        else:
            title, color = tr("Waiting", "等待连接"), "#8796A8"
        self.bridge_status_title.set(title)
        self.bridge_status_dot.configure(fg_color=color)
        self.bridge_help.configure(text_color=self.MUTED)

    def _on_udp_check(self):
        """v1.4.3: one-tap UDP live test — UDP ASSOCIATE to the phone SOCKS5 with concurrent DNS queries."""
        self.udp_button.configure(state="disabled", text=tr("Checking UDP...", "正在检测 UDP..."))
        self.log("UDP check started: sending DNS queries via the phone SOCKS5 UDP relay in candidate-pool order...")
        threading.Thread(target=self._do_udp_check, daemon=True).start()

    def _do_udp_check(self):
        try:
            ip = self.phone_ip.get().strip()
            port = int(self.phone_port.get().strip() or "8080")
        except ValueError:
            self.root.after(0, lambda: self._udp_check_done(False, "invalid port"))
            return
        # v1.4.81: dual-DNS retry (one 5s shot at one DNS was too brittle).
        # v1.4.93: probe the whole pool in order and REMEMBER the first one
        # that answers, so later checks start there.
        # v1.5.1 BUGFIX: the automatic TUN-adapter DNS reorder is REMOVED.
        # A single successful probe only proves "this resolver answered once
        # over the relay" — promoting it on the adapter made Windows switch
        # its whole resolution path on a one-off result while the phone-side
        # self-test reported a DIFFERENT resolver (the "DNS mismatch yet matched" report),
        # a plausible driver for game flapping. The probe pool
        # is now strictly DIAGNOSTIC: it decides nothing but the sticky
        # probe order; the adapter DNS stays exactly what start() configured.
        primary = getattr(self, "_udp_dns_primary", None)
        pool = ([primary] if primary else []) + [d for d in UDP_CHECK_DNS_POOL if d != primary]
        failures = []
        for idx, dns in enumerate(pool):
            label = tr("last success", "上次成功") if (idx == 0 and primary) else tr("candidate {i}", "候选{i}").format(i=idx + 1)
            self.log(f"UDP check ({label}): sending DNS query via phone SOCKS5 UDP relay to {dns}:53...")
            ok, detail = socks5_udp_dns_check(
                ip, port, dns_ip=dns,
                socks_user=getattr(self, "_socks_user", None) or DEFAULT_PHONE_USER,
                socks_pass=getattr(self, "_socks_pass", None) or DEFAULT_PHONE_PASS,
            )
            if ok:
                if not primary and idx > 0:
                    self.log(f"{dns} probe succeeded; this session's check will prefer it (diagnostics only).")
                self._udp_dns_primary = dns
                self.root.after(0, lambda d=dns: self._udp_check_done(True, f"UDP OK ({d})"))
                return
            failures.append(f"{dns}: {detail}")
            self.log(f"{dns} probe failed ({detail}); auto-switching to the next candidate...")
        combined = "；".join(failures)
        self.root.after(0, lambda: self._udp_check_done(False, combined))

    def _udp_check_done(self, ok, detail):
        self.udp_button.configure(state="normal", text=tr("Check UDP Connectivity", "检测 UDP 连通性"))
        if ok:
            self.log(f"✓ UDP reachable: {detail}")
        else:
            self.log(f"✗ UDP unreachable: {detail} (UDP traffic such as games will not work)")

    def _toggle_advanced(self):
        self._advanced_open = not self._advanced_open
        if self._advanced_open:
            self.advanced_frame.grid(row=5, column=0, sticky="ew", padx=16, pady=(4, 0))
            self.advanced_button.configure(text=tr("Advanced  ⌃", "高级设置  ⌃"))
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text=tr("Advanced  ⌄", "高级设置  ⌄"))

    def _toggle_activity(self, show=None):
        self._activity_open = (not self._activity_open) if show is None else show
        if self._activity_open:
            self.activity_body.pack(fill="x")
            self.activity_button.configure(text=tr("Hide Log", "收起日志"))
        else:
            self.activity_body.pack_forget()
            self.activity_button.configure(text=tr("Show Log", "显示日志"))

    def _set_status(self, text, state="disconnected"):
        colors = {
            "connected": "#23B885",
            "starting": "#E59A31",
            "stopping": "#E59A31",
            "disconnected": "#8796A8",
            "error": "#E45D63",
        }
        details = {
            "connected": tr("Connected. You can share to other devices in step 2.", "连接已就绪，可以在第 2 步共享给其他设备。"),
            "starting": tr("Creating the LinkHelm adapter and configuring routes...", "正在创建 LinkHelm 适配器并配置路由..."),
            "stopping": tr("Closing the tunnel and restoring network settings...", "正在关闭隧道并恢复网络设置..."),
            "disconnected": tr("Enter the phone IP, then tap Connect.", "输入手机 IP 后点击「连接」。"),
            "error": tr("Check the activity log for the cause, then retry.", "请查看活动日志中的原因后重试。"),
        }
        self.status_var.set(text.removeprefix("状态：").removeprefix("Status: "))
        self.status_dot.configure(fg_color=colors.get(state, "#8796A8"))
        self.status_detail.set(details.get(state, ""))
        if state in ("starting", "stopping"):
            self.progress.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 10))
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()
        if state == "error" and not self._activity_open:
            self._toggle_activity(show=True)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_count = 0
        self._gui_log_pending = 0

    def log(self, msg):
        try:
            # v1.5.1 bugfix: timestamp captured HERE, in the calling thread.
            # It used to be formatted inside _log() — i.e. whenever the Tk
            # main loop got around to running the after-callback — so queued
            # log lines could render out of order (real device: 22:10:29 shown before
            # 22:10:28), which made cause->effect chains unreadable.
            stamp = time.strftime('%H:%M:%S')

            # v1.5.4 (P0): bounded producer→GUI queue. If the Tk main loop is
            # ever stalled (modal dialog, system hiccup), worker threads must
            # never queue unboundedly — past 1024 pending lines the message
            # goes to the console only and the TCP/UDP threads keep moving.
            if getattr(self, "_gui_log_pending", 0) >= 1024:
                print(f"[{stamp}] {msg}")
                return
            self._gui_log_pending = getattr(self, "_gui_log_pending", 0) + 1

            def _log():
                self._gui_log_pending = max(0, getattr(self, "_gui_log_pending", 1) - 1)
                tag = None
                if msg.startswith(("✗", "Error", "错误", "失败")):
                    tag = "error"
                elif msg.startswith("✓"):
                    tag = "success"

                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"[{stamp}] {msg}\n", tag)
                self.log_text.see("end")
                # v1.5.0 (P0): bounded log widget; v1.5.4: tightened to a
                # 200-line ring (prune at 220 down to 200). Long tray sessions
                # no longer grow the Text past the ring — the old 1600/400
                # batching let the widget reach 1600 lines, which scrolled
                # noticeably on slow machines.
                self._log_count = getattr(self, "_log_count", 0) + 1
                if self._log_count >= 220:
                    self.log_text.delete("1.0", "end-200lines")
                    self._log_count -= 200
                self.log_text.configure(state="disabled")

            self.root.after(0, _log)
        except:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _bridge_mode_changed(self, choice):
        ethernet = choice == tr("Ethernet", "网线直连")
        locked = self._bridge_busy or self.ics_mgr.needs_cleanup
        if ethernet:
            self.wifi_row.grid_remove()
            self.ethernet_row.grid()
            state = "disabled" if locked else "readonly"
            self.ethernet_choice.configure(state=state)
            self.refresh_ethernet_button.configure(
                state="disabled" if locked else "normal"
            )
        else:
            self.ethernet_row.grid_remove()
            self.wifi_row.grid()
            self.ethernet_choice.configure(state="disabled")
            self.refresh_ethernet_button.configure(state="disabled")
        if not self.ics_mgr.needs_cleanup:
            healthy = bool(self.tun_mgr and self.tun_mgr.is_healthy())
            if not healthy:
                self.bridge_status.set("Connect the phone in Step 1 first.")
            else:
                self.bridge_status.set(
                    "Connect the cable, refresh the adapter, then start sharing."
                    if ethernet else
                    "Windows will enable the Mobile Hotspot automatically for connected devices."
                )

    def _refresh_ethernet(self):
        self.refresh_ethernet_button.configure(state="disabled")
        threading.Thread(target=self._do_refresh_ethernet, daemon=True).start()

    # ---- v1.5.45 (beta1.0.1): file-transfer trial -------------------------
    # beta1.0.3: the dialog now shows the ACTUAL local IP (dynamic — it
    # follows the current Wi-Fi Direct lease), takes the peer's IP as input,
    # and requires one-tap mutual IP pairing before any transfer.
    def _open_file_transfer_dialog(self):
        phone_ip = self.phone_ip.get().strip() or "192.168.49.1"
        user = self.phone_user.get().strip() or DEFAULT_PHONE_USER
        password = self.phone_pass.get().strip() or DEFAULT_PHONE_PASS
        local_ip = file_transfer.local_ip_toward(phone_ip)

        win = ctk.CTkToplevel(self.root)
        win.title(tr("File Transfer (trial)", "文件传输（试用版）"))
        win.geometry("470x470")
        win.transient(self.root)
        win.grab_set()

        ctk.CTkLabel(
            win, font=("Segoe UI", 12, "bold"), text_color=self.TEXT,
            text=tr("Single file up to 1 GB, over the Wi-Fi Direct link only.\n"
                    "Never touches the phone's internet exit; SHA-256 verified.",
                    "单文件最大 1 GB，仅走 Wi-Fi Direct 本地通道。\n不经手机外网出口；传输后校验 SHA-256。")
        ).pack(padx=16, pady=(14, 4))
        local_label = ctk.CTkLabel(
            win, font=("Segoe UI", 10), text_color=self.TEXT,
            text=tr(f"This device address (enter it on the other side): {local_ip}:{file_transfer.FT_PORT}",
                    f"本机地址（填到另一台设备）：{local_ip}:{file_transfer.FT_PORT}")
        )
        local_label.pack(padx=16, pady=(0, 2))
        ctk.CTkLabel(
            win, font=("Segoe UI", 9), text_color=self.MUTED,
            text=tr("The IP changes when the Wi-Fi Direct group is rebuilt — always follow the currently displayed value.",
                    "IP 随 Wi-Fi Direct 组重建而变化，请以当前显示为准。")
        ).pack(padx=16, pady=(0, 6))

        # beta1.0.3: peer IP + one-tap mutual pairing.
        peer_frame = ctk.CTkFrame(win, fg_color="transparent")
        peer_frame.pack(fill="x", padx=16, pady=(2, 0))
        peer_entry = ctk.CTkEntry(peer_frame, width=250, placeholder_text=phone_ip)
        peer_entry.insert(0, phone_ip)
        peer_entry.pack(side="left", padx=(0, 6))
        pair_state = {"paired": file_transfer.is_paired(phone_ip)}
        pair_btn = ctk.CTkButton(
            peer_frame, width=110,
            fg_color=self.ACCENT, hover_color=self.STATUS_BG, text_color="#FFFFFF",
            command=lambda: None)  # replaced below
        pair_btn.pack(side="left")

        def _refresh_pair_widgets():
            ip = peer_entry.get().strip() or phone_ip
            pair_state["paired"] = file_transfer.is_paired(ip)
            if pair_state["paired"]:
                pair_btn.configure(text=tr("Re-pair", "重新配对"))
                pair_tip.configure(text=tr("✓ Paired with this address", "✓ 已与该地址配对"),
                                   text_color="#4ADE80")
            else:
                pair_btn.configure(text=tr("Pair", "配对"))
                pair_tip.configure(
                    text=tr("Pair before transferring (both ends tap Pair)", "传输前需配对（两端都要点击配对）"),
                    text_color="#F87171")
            send_btn.configure(
                state="normal" if pair_state["paired"] else "disabled")

        def _do_pair():
            ip = peer_entry.get().strip() or phone_ip
            pair_btn.configure(state="disabled", text=tr("Pairing...", "配对中…"))

            def worker():
                err = file_transfer.pair_with(ip, user, password, log_fn=self.log)
                self.root.after(0, lambda: pair_btn.configure(state="normal"))
                self.root.after(0, _refresh_pair_widgets)
                if err:
                    _set_status(err, ok=False)
                else:
                    _set_status(tr(f"Paired with {ip}.", f"已与 {ip} 完成配对。"), ok=True)

            threading.Thread(target=worker, daemon=True).start()

        pair_btn.configure(command=_do_pair)
        pair_tip = ctk.CTkLabel(win, font=("Segoe UI", 10), text_color=self.MUTED, text="")
        pair_tip.pack(padx=16, pady=(4, 0))

        status = ctk.CTkLabel(win, font=("Segoe UI", 10), text_color=self.MUTED,
                              text=tr("Idle", "空闲"))
        status.pack(padx=16, pady=(4, 2))
        bar = ctk.CTkProgressBar(win, width=430)
        bar.set(0)
        bar.pack(padx=16, pady=(0, 10))

        cancel_event = threading.Event()
        self._ft_server = None

        def _set_status(text, ok=None):
            color = self.MUTED
            if ok is True:
                color = "#4ADE80"
            elif ok is False:
                color = "#F87171"
            self.root.after(0, lambda: status.configure(text=text, text_color=color))

        def _progress(done, total):
            frac = (done / total) if total else 0.0
            self.root.after(0, lambda: bar.set(frac))
            self.root.after(0, lambda: status.configure(
                text=f"{done // 1024 // 1024} MB / {total // 1024 // 1024} MB",
                text_color=self.MUTED))

        def _do_send():
            ip = peer_entry.get().strip() or phone_ip
            if not file_transfer.is_paired(ip):
                _set_status(tr("Pair with the peer first.", "请先与对方配对。"), ok=False)
                return
            path = filedialog.askopenfilename(
                parent=win, title=tr("Pick a file to send (<= 1 GB)", "选择要发送的文件（≤ 1 GB）"))
            if not path:
                return
            cancel_event.clear()

            def worker():
                try:
                    file_transfer.send_file(
                        ip, user, password, path,
                        progress_cb=_progress, cancel_event=cancel_event, log_fn=self.log)
                    _set_status(tr("Sent and verified.", "已发送并校验。"), ok=True)
                    self.root.after(0, lambda: bar.set(1.0))
                except Exception as exc:
                    _set_status(str(exc), ok=False)

            threading.Thread(target=worker, daemon=True).start()
            _set_status(tr("Sending...", "正在发送…"))

        def _do_receive():
            path = filedialog.asksaveasfilename(
                parent=win, title=tr("Save the incoming file as", "接收文件保存为"),
                initialfile="received.bin")
            if not path:
                return
            self._ft_server = file_transfer.ReceiverServer(
                local_ip, user, password, path, log_fn=self.log)
            self._ft_server.start()
            bar.set(0)
            _set_status(tr("Waiting for the phone to send (stops after one file)...",
                           "等待手机发送（接收一个文件后自动停止）…"))

        def _cancel():
            cancel_event.set()
            if self._ft_server:
                self._ft_server.stop()
                self._ft_server = None
            _set_status(tr("Cancelled.", "已取消。"), ok=False)

        def _close():
            cancel_event.set()
            if self._ft_server:
                self._ft_server.stop()
                self._ft_server = None
            win.destroy()

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(6, 14))
        send_btn = ctk.CTkButton(
            btn_row, text=tr("Send a file to the peer", "发送文件到对方"),
            fg_color=self.ACCENT, hover_color=self.STATUS_BG, text_color="#FFFFFF",
            command=_do_send)
        send_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            btn_row, text=tr("Receive from the peer", "从对方接收文件"),
            fg_color=self.STATUS_BG, hover_color=self.CARD, text_color=self.ACCENT,
            command=_do_receive).pack(side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(
            btn_row, text=tr("Cancel", "取消"), width=60,
            fg_color="transparent", hover_color=self.CARD, text_color=self.MUTED,
            command=_cancel).pack(side="left", padx=(4, 0))
        win.protocol("WM_DELETE_WINDOW", _close)
        _refresh_pair_widgets()

    def _do_refresh_ethernet(self):
        try:
            adapters = self.ics_mgr.list_ethernet()
            self.root.after(0, lambda: self._show_ethernet(adapters))
        except BridgeError as error:
            self.log(f"Could not list Ethernet adapters: {error}")
            self.root.after(0, lambda: self.bridge_status.set(f"{tr('Error: ', '错误：')}{error}"))
        finally:
            self.root.after(0, lambda: self.refresh_ethernet_button.configure(
                state="normal" if self.bridge_mode.get() == tr("Ethernet", "网线直连")
                and not (self._bridge_busy or self.ics_mgr.needs_cleanup) else "disabled"
            ))

    def _show_ethernet(self, adapters):
        self._ethernet_choices = {
            f"{item.name} ({item.guid[:8]})": item.guid for item in adapters
        }
        values = list(self._ethernet_choices) or ["Connect cable, then refresh"]
        self.ethernet_choice.configure(values=values)
        self.ethernet_choice.set(values[0])

    def _update_bridge_button(self):
        locked = self._bridge_busy or self.ics_mgr.needs_cleanup
        healthy = bool(self.tun_mgr and self.tun_mgr.is_healthy())
        self.bridge_mode.configure(state="disabled" if locked or not healthy else "normal")
        self.ethernet_choice.configure(
            state="disabled" if locked or not healthy or self.bridge_mode.get() != tr("Ethernet", "网线直连")
            else "readonly"
        )
        self.refresh_ethernet_button.configure(
            state="disabled" if locked or not healthy or self.bridge_mode.get() != tr("Ethernet", "网线直连")
            else "normal"
        )
        self.bridge_eyebrow.configure(text_color=self.ACCENT if healthy else ("#628EBE", "#8296AC"))
        self.bridge_title.configure(text_color=self.TEXT if healthy else self.MUTED)
        self.bridge_subtitle.configure(text_color=self.MUTED if healthy else self.SUBTLE)
        if self._bridge_busy:
            self.bridge_button.configure(
                state="disabled", fg_color=self.BORDER, text=tr("Working...", "处理中...")
            )
        elif self.ics_mgr.needs_cleanup:
            self.bridge_button.configure(
                state="normal", fg_color=self.ACCENT, text=tr("Stop Sharing", "停止共享网络")
            )
        else:
            self.bridge_button.configure(
                state="normal" if healthy else "disabled",
                text=tr("Start Sharing", "开始共享网络"),
                fg_color=self.ACCENT if healthy else ("#BFD4E9", "#344B63")
            )

    def _stop_bridge(self):
        if self._bridge_busy or not self.ics_mgr.needs_cleanup:
            return
        self._bridge_busy = True
        self.bridge_status.set(tr("Stopping sharing...", "正在停止共享..."))
        self._update_bridge_button()
        threading.Thread(target=self._do_bridge_stop, daemon=True).start()

    def _toggle_bridge(self):
        if self._bridge_busy or self.ics_mgr.needs_cleanup:
            return
        # v1.5.41: silent UX — precondition hints go to the in-app status
        # line + log instead of modal dialogs (no more popups interrupting the flow).
        if not self.tun_mgr or not self.tun_mgr.is_healthy():
            self.log("Sharing not started: connect first (step 1).")
            self.bridge_status.set(tr("Connect first, then start sharing.", "请先连接后再开始共享。"))
            return
        mode = "ethernet" if self.bridge_mode.get() == tr("Ethernet", "网线直连") else "wifi"
        guid = self._ethernet_choices.get(self.ethernet_choice.get()) if mode == "ethernet" else None
        if mode == "ethernet" and not guid:
            self.log("Sharing not started: connect the cable and refresh the adapter first.")
            self.bridge_status.set(tr("Plug in the cable and refresh the adapter first.", "请先连接网线并刷新适配器。"))
            return
        self._bridge_busy = True
        self.bridge_status.set(tr("Configuring Windows sharing...", "正在配置 Windows 共享..."))
        self._update_bridge_button()
        threading.Thread(
            target=self._do_bridge_start, args=(mode, guid), daemon=True
        ).start()

    def _do_bridge_start(self, mode, guid):
        try:
            with self._bridge_lock:
                if not self.tun_mgr or not self.tun_mgr.is_healthy():
                    raise BridgeError("Bridge channel state is abnormal.")
                details = self.ics_mgr.start(mode, guid)
                if not self.tun_mgr.is_healthy():
                    self.ics_mgr.stop()
                    raise BridgeError("The connection dropped while starting sharing.")
            self.root.after(0, lambda: self._show_bridge_started(details))
        except Exception as error:
            self.log(f"Sharing failed: {error}")
            self.root.after(0, lambda: self.bridge_status.set(f"{tr('Error: ', '错误：')}{error}"))
        finally:
            self._bridge_busy = False
            self.root.after(0, self._update_bridge_button)

    def _show_bridge_started(self, details):
        if not self.ics_mgr.active:
            return
        if details.mode == "wifi":
            if details.ssid and details.password:
                self._set_hotspot_info(details.ssid, details.password)
                self.bridge_status.set(
                    f"Connect devices to Wi-Fi '{details.ssid}', password '{details.password}', "
                    f"keep device proxy OFF."
                )
            else:
                self.bridge_status.set(
                    tr("Connect devices to the Windows Mobile Hotspot (name & password in Windows Settings); keep device proxy off.", "请将设备接入 Windows 移动热点，名称和密码见 Windows 设置，设备代理设置保持关闭。")
                )
        else:
            self.bridge_status.set(
                f"Connect devices via Ethernet to '{details.output_name}', "
                f"set IP and DNS to automatic, keep device proxy OFF."
            )

    def _do_bridge_stop(self):
        try:
            with self._bridge_lock:
                self.ics_mgr.stop()
            self.root.after(0, lambda: self.bridge_status.set(tr("Sharing stopped.", "共享已停止。")))
        except Exception as error:
            self.log(f"Failed to stop sharing: {error}")
            self.root.after(0, lambda: self.bridge_status.set(f"{tr('Cleanup error: ', '清理错误：')}{error}"))
        finally:
            self._bridge_busy = False
            self.root.after(0, self._update_bridge_button)

    def _on_tunnel_lost(self):
        with self._bridge_lock:
            if self.ics_mgr.needs_cleanup:
                self.ics_mgr.stop()
                self.log("Connection channel dropped; sharing stopped automatically.")
                self.root.after(
                    0, lambda: self.bridge_status.set(
                        tr("Link lost, sharing stopped; reconnect, then start sharing again.", "连接断开，共享已停止；请重新连接后再开始共享。")
                    )
                )
        self.root.after(0, self._update_bridge_button)

    def start(self):
        ip = self.phone_ip.get().strip()
        # v1.5.41: silent UX — input problems are reported in the in-app log
        # and status line instead of modal dialogs (silent start, no popups).
        try:
            p_port = int(self.phone_port.get().strip())
            l_port = int(self.local_port.get().strip())
        except ValueError:
            self.log("Invalid input: the port must be a number.")
            self._set_status(tr("Status: Invalid input (ports must be numbers)", "状态：输入有误（端口必须是数字）"), "error")
            return

        # v1.3: the local HTTP->SOCKS bridge port must not collide with the phone proxy.
        if not (0 < p_port < 65536) or not (0 < l_port < 65535 + 1):
            self.log("Invalid input: the port must be between 1 and 65535.")
            self._set_status(tr("Status: Invalid input (ports must be 1-65535)", "状态：输入有误（端口 1-65535）"), "error")
            return
        if p_port == l_port:
            self.log("Invalid input: the local download bridge port cannot equal the phone SOCKS5 port.")
            self._set_status(tr("Status: Invalid input (port conflict)", "状态：输入有误（端口冲突）"), "error")
            return

        # v1.5.47: SOCKS5 credentials — 8-13 alnum chars, matching the phone.
        s_user = self.phone_user.get().strip()
        s_pass = self.phone_pass.get().strip()
        if not (SOCKS_CREDENTIAL_REGEX.match(s_user)
                and SOCKS_CREDENTIAL_REGEX.match(s_pass)):
            self.log("Invalid input: the SOCKS5 user/password must be 8-13 alphanumeric characters.")
            self._set_status(tr("Status: Invalid input (credentials must be 8-13 alphanumeric chars)", "状态：输入有误（账号/密码 8-13 位字母数字）"), "error")
            return

        self.btn_start.configure(
            state="disabled", fg_color=self.BORDER,
            text_color_disabled=self.MUTED
        )
        self.btn_stop.configure(state="normal")
        for field in (self.phone_ip, self.phone_port, self.local_port,
                      self.phone_user, self.phone_pass):
            field.configure(state="disabled")
        self._set_status(tr("Status: Connecting...", "状态：连接中..."), "starting")
        self.progress.start()
        threading.Thread(
            target=self._do_start, args=(ip, p_port, l_port, s_user, s_pass),
            daemon=True
        ).start()

    # --- v1.5.44: Settings panel (settings window; openable from tray "Settings" or the main UI) ---
    def open_settings(self):
        """Single-instance settings window. The Task Scheduler REAL state is
        the source of truth for start-at-login; settings.json stores auto-connect."""
        win = getattr(self, "_settings_win", None)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        self._settings_win = win
        win.title(tr("LinkHelm Settings", "LinkHelm 设置"))
        win.geometry("460x760")
        win.minsize(430, 700)
        win.configure(fg_color=self.BG)
        win.transient(self.root)

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(14, 14))

        ctk.CTkLabel(
            body, text=tr("Settings", "设置"), font=("Segoe UI", 19, "bold"),
            text_color=self.TEXT, anchor="w"
        ).pack(fill="x", pady=(0, 10))

        # ── Language (UI i18n) ───────────────────────────────────────
        lang_card = ctk.CTkFrame(
            body, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        lang_card.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            lang_card, text=tr("Language", "语言"), font=("Segoe UI", 12, "bold"),
            text_color=self.TEXT, anchor="w"
        ).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            lang_card,
            text=tr("Interface language. Changes apply immediately after a quick restart.",
                    "Interface language. Changes apply immediately after a quick restart."),
            font=("Segoe UI", 11), text_color=self.MUTED, anchor="w",
            wraplength=390, justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 8))
        lang_var = ctk.StringVar(value=("中文" if LANG == "zh" else "English"))
        ctk.CTkSegmentedButton(
            lang_card, values=["English", "中文"], variable=lang_var,
            command=lambda _v: _on_language_change()
        ).pack(anchor="w", padx=14, pady=(0, 12))

        def _on_language_change():
            chosen = lang_var.get()
            new_lang = "zh" if chosen == "中文" else "en"
            if new_lang == LANG:
                return
            settings = load_app_settings()
            settings["language"] = new_lang
            try:
                save_app_settings(settings)
            except Exception:
                pass
            # Relaunch so the main window AND tray both pick up the new language.
            try:
                import subprocess as _sp
                _sp.Popen([sys.executable] + sys.argv)
            except Exception:
                pass
            os._exit(0)

        status_var = ctk.StringVar(value=tr("Ready.", "就绪。"))

        def _set_status(text):
            status_var.set(text)

        # --- Toggle 1: start at login ---------------------------------------------
        card1 = ctk.CTkFrame(
            body, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        card1.pack(fill="x", pady=(0, 10))
        sw_start = ctk.CTkSwitch(
            card1, text=tr("Start at logon (launch automatically after Windows sign-in)", "开机自启动（登录 Windows 后自动启动）"),
            command=lambda: _on_start_at_logon(), progress_color=self.ACCENT
        )
        sw_start.pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            card1, text=tr("When enabled, starts silently at logon minimized to the tray; when disabled, no auto-start.", "开启后登录时静默启动并最小化到托盘；关闭则不自动启动。"),
            font=("Segoe UI", 11), text_color=self.MUTED, anchor="w", wraplength=390,
            justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # Initial state from the local preference, then verified against the
        # REAL Task Scheduler state in the background (never block the UI).
        sw_start.select() if load_app_settings().get("startAtLogon") else sw_start.deselect()

        def _verify_login_task():
            real = _schtasks_task_exists(SCHTASKS_LOGIN_NAME)
            self.root.after(
                0, lambda: (sw_start.select if real else sw_start.deselect)()
            )
        threading.Thread(target=_verify_login_task, daemon=True).start()

        def _on_start_at_logon():
            enabled = bool(sw_start.get())
            sw_start.configure(state="disabled")
            _set_status(tr("Applying start-at-logon setting...", "正在应用开机自启动设置…") if enabled else tr("Disabling start-at-logon...", "正在关闭开机自启动…"))

            def worker():
                ok = _sync_login_startup(enabled, log_fn=self.log)
                # The REAL task state decides what the switch shows.
                real = _schtasks_task_exists(SCHTASKS_LOGIN_NAME)
                settings = load_app_settings()
                settings["startAtLogon"] = bool(real)
                try:
                    save_app_settings(settings)
                except Exception as error:
                    self.log(f"Settings save failed: {error}")

                def apply():
                    (sw_start.select if real else sw_start.deselect)()
                    sw_start.configure(state="normal")
                    _set_status(
                        "Autostart enabled (takes effect at next sign-in)." if real
                        else (tr("Start-at-logon disabled.", "已关闭开机自启动。") if ok else tr("Operation failed. Retry as administrator.", "操作失败，请以管理员身份重试。"))
                    )
                self.root.after(0, apply)

            threading.Thread(target=worker, daemon=True).start()

        # --- Toggle 2: auto-connect ------------------------------------------------
        card2 = ctk.CTkFrame(
            body, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        card2.pack(fill="x", pady=(0, 10))
        sw_conn = ctk.CTkSwitch(
            card2, text=tr("Auto-connect (start automatically when the phone proxy is detected)", "自动连接（启动后检测到手机代理就自动开始）"),
            command=lambda: _on_connect_on_launch(), progress_color=self.ACCENT
        )
        sw_conn.pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            card2, text=tr("Only applies to the silent start at logon: waits up to 120 seconds, then stays in the tray.", "仅对「开机自启动」的静默启动生效：最多等待 120 秒，超时则保持托盘待命。"),
            font=("Segoe UI", 11), text_color=self.MUTED, anchor="w", wraplength=390,
            justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 12))

        sw_conn.select() if load_app_settings().get("connectOnLaunch") else sw_conn.deselect()

        def _on_connect_on_launch():
            enabled = bool(sw_conn.get())
            settings = load_app_settings()
            settings["connectOnLaunch"] = enabled
            try:
                save_app_settings(settings)
                self.log(tr("Auto-connect enabled (connects automatically after logon start).", "自动连接已开启（登录启动后自动连接手机代理）。") if enabled else tr("Auto-connect disabled.", "自动连接已关闭。"))
                _set_status(tr("Auto-connect enabled.", "自动连接已开启。") if enabled else tr("Auto-connect disabled.", "自动连接已关闭。"))
            except Exception as error:
                self.log(f"Settings save failed: {error}")
                _set_status(tr("Save failed: ", "保存失败：") + str(error))

        # --- v1.5.47: SOCKS5 account (fixed initial value, manual edit only) --------
        card_auth = ctk.CTkFrame(
            body, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        card_auth.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            card_auth, text=tr("SOCKS5 Account", "SOCKS5 账户"), font=("Segoe UI", 12, "bold"),
            text_color=self.TEXT, anchor="w"
        ).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            card_auth,
            text=tr("The initial credentials match the Android app and are fixed (never randomized). Edit here only: 8-13 alphanumeric characters. Changes apply immediately (the tunnel rebuilds automatically while connected).", "默认初始账号密码与安卓端一致且固定（不随机刷新），只能在此手动修改：8-13 位英文字母或数字。修改后立即生效（连接中会自动重建隧道）。"),
            font=("Segoe UI", 11), text_color=self.MUTED, anchor="w",
            wraplength=390, justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 8))

        cred_row = ctk.CTkFrame(card_auth, fg_color="transparent")
        cred_row.pack(fill="x", padx=14)
        cred_row.grid_columnconfigure(0, weight=1)
        cred_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cred_row, text=tr("User", "账号"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=16).grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        ctk.CTkLabel(cred_row, text=tr("Password", "密码"), text_color=self.TEXT,
                     font=("Segoe UI", 10, "bold"), anchor="w", height=16).grid(
            row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 2)
        )
        cur = load_app_settings()
        ent_user = ctk.CTkEntry(
            cred_row, height=30, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER
        )
        ent_user.insert(0, cur.get("socksUser") or DEFAULT_PHONE_USER)
        ent_user.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ent_pass = ctk.CTkEntry(
            cred_row, height=30, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER
        )
        ent_pass.insert(0, cur.get("socksPass") or DEFAULT_PHONE_PASS)
        ent_pass.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(0, 8))

        def _on_save_creds():
            u = ent_user.get().strip()
            p = ent_pass.get().strip()
            if not (SOCKS_CREDENTIAL_REGEX.match(u)
                    and SOCKS_CREDENTIAL_REGEX.match(p)):
                _set_status(tr("Save failed: user and password must be 8-13 alphanumeric characters.", "保存失败：账号和密码必须为 8-13 位英文字母或数字。"))
                return
            ent_user.configure(state="disabled")
            ent_pass.configure(state="disabled")
            _set_status(tr("Saving SOCKS5 account...", "正在保存 SOCKS5 账户…"))

            def worker():
                settings = load_app_settings()
                settings["socksUser"] = u
                settings["socksPass"] = p
                error = None
                try:
                    save_app_settings(settings)
                except Exception as save_error:
                    error = save_error
                # Sync the main-window fields (they stay editable when idle)
                # so the next connect uses the new credentials without retyping.
                def apply():
                    try:
                        self.phone_user.delete(0, "end")
                        self.phone_user.insert(0, u)
                        self.phone_pass.delete(0, "end")
                        self.phone_pass.insert(0, p)
                    except Exception:
                        pass
                    ent_user.configure(state="normal")
                    ent_pass.configure(state="normal")
                    if error:
                        _set_status(tr("Save failed: ", "保存失败：") + str(error))
                        return
                    _set_status(tr("SOCKS5 account saved and applied immediately.", "SOCKS5 账户已保存并立即生效。"))
                    self.log("SOCKS5 account manually edited (8-13 alphanumeric); applied immediately.")
                self.root.after(0, apply)
                # Live apply: if a tunnel is up, swap the credentials it dials
                # with and rebuild it (tun2socks reads the URL only at launch).
                if error is None and self.tun_mgr is not None:
                    self.tun_mgr.socks_user = u
                    self.tun_mgr.socks_pass = p
                    self._socks_user = u
                    self._socks_pass = p
                    try:
                        if self.tun_mgr._monitoring_active:
                            self.root.after(0, lambda: self.log(
                                tr("Detected an active connection; rebuilding the tunnel with the new account...", "检测到连接运行中，正在用新账户重建隧道...")
                            ))
                            self.tun_mgr._restart_tun2socks()
                    except Exception as live_error:
                        self.log("Live apply failed (" + str(live_error) + "); the new account will be used at the next connection.")
            threading.Thread(target=worker, daemon=True).start()

        ctk.CTkButton(
            card_auth, text=tr("Save Account", "保存账户"), width=110, command=_on_save_creds,
            fg_color=self.ACCENT, hover_color="#0057A8"
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # --- ICS read-only diagnostics ---------------------------------------------
        card3 = ctk.CTkFrame(
            body, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        card3.pack(fill="both", expand=True, pady=(0, 10))
        ctk.CTkLabel(
            card3, text=tr("ICS Diagnostics (read-only, changes nothing)", "ICS 诊断（只读，不修改任何设置）"),
            font=("Segoe UI", 12, "bold"), text_color=self.TEXT, anchor="w"
        ).pack(anchor="w", padx=14, pady=(12, 4))

        diag_box = ctk.CTkTextbox(
            card3, height=170, font=("Consolas", 11),
            fg_color=self.STATUS_BG, text_color=self.TEXT
        )
        diag_box.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        diag_box.insert("1.0", tr("Click \u201cRun Diagnostics\u201d to check the ICS service, sharing roles, and the route to the phone.", "点击「运行诊断」检查 ICS 服务、共享角色与到手机的路由。"))
        diag_box.configure(state="disabled")

        def _run_ics_diag():
            _set_status(tr("Running read-only ICS diagnostics...", "正在运行 ICS 只读诊断…"))
            btn_diag.configure(state="disabled")

            def worker():
                report = self._ics_diagnose(
                    self.phone_ip.get().strip() or "192.168.49.1"
                )

                def show():
                    diag_box.configure(state="normal")
                    diag_box.delete("1.0", "end")
                    diag_box.insert("1.0", report)
                    diag_box.configure(state="disabled")
                    _set_status(tr("ICS diagnostics complete (read-only, nothing changed).", "ICS 诊断完成（只读，未做任何修改）。"))
                    btn_diag.configure(state="normal")
                self.root.after(0, show)

            threading.Thread(target=worker, daemon=True).start()

        btn_diag = ctk.CTkButton(
            card3, text=tr("Run Diagnostics", "运行诊断"), width=110, command=_run_ics_diag,
            fg_color=self.ACCENT, hover_color="#0057A8"
        )
        btn_diag.pack(anchor="w", padx=14, pady=(0, 12))

        ctk.CTkLabel(
            body, textvariable=status_var, font=("Segoe UI", 11),
            text_color=self.MUTED, anchor="w", wraplength=410, justify="left"
        ).pack(fill="x", pady=(0, 4))

    def _ics_diagnose(self, phone_ip):
        """v1.5.44: READ-ONLY ICS diagnostic (docx §7). Reports:
        SharedAccess service state, per-adapter ICS roles (COM HNetCfg), and
        the route to the phone. NEVER modifies anything."""
        ps = (
            "$svc = Get-Service SharedAccess -ErrorAction SilentlyContinue; "
            "if ($svc) { Write-Output ('[Service] SharedAccess: ' + $svc.Status + ' / ' + $svc.StartType) } "
            "else { Write-Output '[Service] SharedAccess: not installed' }; "
            "try { "
            "$ns = New-Object -ComObject HNetCfg.HNetShare; "
            "foreach ($c in $ns.EnumEveryConnection) { "
            "$cfg = $ns.INetSharingConfigurationForINetConnection.Invoke($c); "
            "$props = $ns.NetConnectionProps.Invoke($c); "
            "$role = 'sharing not enabled'; "
            "if ($cfg.SharingEnabled) { "
            "if ($cfg.SharingConnectionType -eq 0) { $role = 'ICS public (outbound source)' } "
            "elseif ($cfg.SharingConnectionType -eq 1) { $role = 'ICS private (shared to)' } "
            "else { $role = ('ICS enabled, type ' + $cfg.SharingConnectionType) } }; "
            "Write-Output ('[Sharing] ' + $props.Name + ' | ' + $props.DeviceName + ' | ' + $role) } "
            "} catch { Write-Output ('[Sharing] enumeration failed: ' + $_.Exception.Message) }; "
            "try { "
            "$rt = Find-NetRoute -RemoteIPAddress '" + phone_ip + "' -ErrorAction Stop | Select-Object -First 1; "
            "Write-Output ('[Route] to phone " + phone_ip + " via ' + $rt.InterfaceAlias + ' (ifIndex ' + $rt.InterfaceIndex + ')') "
            "} catch { Write-Output ('[Route] to phone " + phone_ip + " has no valid route: ' + $_.Exception.Message) }"
        )
        try:
            rc, out, err = _run_decoded(
                ["powershell", "-NoProfile", "-Command", ps], timeout=45
            )
            lines = [line for line in (out or "").splitlines() if line.strip()]
            if not lines:
                lines = ["(diagnostics returned no output)"] + ([err.strip()[:200]] if err and err.strip() else [])
            self.log("ICS read-only diagnostics complete.")
            return "\n".join(lines)
        except Exception as error:
            self.log(f"ICS diagnostics failed: {error}")
            return f"Diagnostics failed to run: {error}"

    def _confirm_dependency_download(self):
        """Patch B: explicit user consent before any runtime dependency download."""
        result = {"approved": False}
        event = threading.Event()

        def _ask():
            try:
                result["approved"] = messagebox.askyesno(
                    tr("Download dependencies", "下载依赖"),
                    tr("The built-in tun2socks/Wintun components were not found.\n\n",
                       "未找到内置的 tun2socks/Wintun 组件。\n\n")
                    + tr(f"A locked build of tun2socks/Wintun (tun2socks v{TUN2SOCKS_VERSION}, "
                         f"Wintun v{WINTUN_VERSION}) can be downloaded via the phone proxy. ",
                         f"可通过手机代理下载锁定版本（tun2socks v{TUN2SOCKS_VERSION}，"
                         f"Wintun v{WINTUN_VERSION}），")
                    + tr("Every file is SHA-256 verified before use.\n\n",
                         "），每个文件使用前均经 SHA-256 校验。\n\n")
                    + tr("Allow download?", "是否允许下载？")
                )
            finally:
                event.set()

        self.root.after(0, _ask)
        event.wait()
        return result["approved"]

    def _do_start(self, ip, p_port, l_port, s_user=None, s_pass=None):
        s_user = s_user or DEFAULT_PHONE_USER
        s_pass = s_pass or DEFAULT_PHONE_PASS
        # v1.5.47: remember the active credentials for the UDP diagnostic probe.
        self._socks_user = s_user
        self._socks_pass = s_pass
        try:
            # v1.4.91: connect = reset first. A leftover old TunManager (monitor threads and
            # tun2socks child processes from reconnecting/failed states) makes the new
            # connection "occupied, cannot connect" — every connect click fully stops the old
            # tunnel first, then starts from scratch.
            if self.tun_mgr is not None:
                self.log("Old connection detected; resetting the SOCKS5 tunnel...")
                try:
                    self.tun_mgr.stop()
                except Exception as reset_error:
                    self.log(f"Old connection cleanup failed (continuing new connection): {reset_error}")
                self.tun_mgr = None
            self.log(f"Establishing global connection: {ip}:{p_port}")
            self.tun_mgr = TunManager(
                ip, p_port, l_port, self.log, on_tunnel_lost=self._on_tunnel_lost,
                confirm_download=self._confirm_dependency_download,
                socks_user=s_user, socks_pass=s_pass
            )
            self.tun_mgr.start()
            self.log("Connection established.")
            self.root.after(0, lambda: self._set_status(tr("Status: Connected", "状态：已连接"), "connected"))
            self.root.after(0, self.progress.stop)
            self.root.after(0, self._update_bridge_button)
            self.root.after(0, lambda: self._bridge_mode_changed(self.bridge_mode.get()))
            self.root.after(0, self._refresh_ethernet)
            threading.Thread(target=self._health_status_updater, daemon=True).start()
        except Exception as error:
            self.log(f"Connection start failed: {error}")
            cleaned = False
            try:
                cleaned = self._do_stop(is_closing=True)
            except Exception as cleanup_error:
                self.log(f"Connection cleanup failed: {cleanup_error}")
            def show_error():
                self.progress.stop()
                self._set_status(tr("Connection failed", "连接失败") if cleaned else tr("Cleanup failed", "清理失败"), "error")
                self.btn_start.configure(
                    state="normal" if cleaned else "disabled",
                    fg_color=self.ACCENT if cleaned else self.BORDER
                )
                self.btn_stop.configure(state="disabled" if cleaned else "normal")
                for field in (self.phone_ip, self.phone_port, self.local_port):
                    field.configure(state="normal" if cleaned else "disabled")
                self._update_bridge_button()
            self.root.after(0, show_error)

    def _health_status_updater(self):
        while self.tun_mgr and self.tun_mgr._monitoring_active:
            time.sleep(2)
            if not self.tun_mgr or not self.tun_mgr._monitoring_active:
                break
            if self.tun_mgr.is_healthy():
                self.root.after(0, lambda: self._set_status(tr("Status: Connected", "状态：已连接"), "connected"))
            else:
                self.root.after(0, lambda: self._set_status(tr("Status: Reconnecting...", "状态：重连中..."), "stopping"))
            self.root.after(0, self._update_bridge_button)
        if self.tun_mgr:
            self.root.after(0, lambda: self._set_status(tr("Status: Connection error", "状态：连接异常"), "error"))
            self.root.after(0, self._update_bridge_button)

    def stop(self):
        self._set_status(tr("Status: Stopping...", "状态：停止中..."), "stopping")
        self.progress.start()
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self, is_closing=False):
        try:
            self._on_tunnel_lost()
        except BridgeError as error:
            self.log(f"Stop sharing first: {error}")
            if not is_closing:
                self.root.after(0, self.progress.stop)
                self.root.after(
                    0, lambda: self._set_status(tr("Status: Sharing cleanup failed", "状态：共享清理失败"), "error")
                )
            return False

        if self.tun_mgr:
            self.tun_mgr.stop()
            self.tun_mgr = None
            self.log("Connection stopped.")

        if not is_closing:
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self._set_status(tr("Status: Disconnected", "状态：未连接"), "disconnected"))
            self.root.after(0, lambda: self.btn_start.configure(
                state="normal", fg_color=self.ACCENT
            ))
            self.root.after(0, lambda: self.btn_stop.configure(state="disabled"))
            self.root.after(0, lambda: [field.configure(state="normal") for field in (
                self.phone_ip, self.phone_port, self.local_port,
                self.phone_user, self.phone_pass
            )])
            self.root.after(0, self._update_bridge_button)
            self.root.after(0, lambda: self.bridge_status.set("Connect the phone in step 1 first."))
            self.log("Stopped.")
        return True

def main(startup_hidden=False):
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("dark-blue")

    root = ctk.CTk()
    app = App(root)

    # v1.4.93: tray residency. Design per the 1.4.92 report:
    # - the window is only a control surface; hiding it NEVER touches the
    #   tunnel ("minimizing is not disconnecting");
    # - the tray thread only ENQUEUES actions; every Tk call happens on the
    #   GUI main thread via root.after polling (direct tray→Tk calls would
    #   corrupt Tk state / fake-freeze);
    # - closing the window (X) ASKS: minimize to tray or full exit — never a
    #   silent default; the native minimize button keeps its taskbar
    #   behaviour (do not minimize to tray by default);
    # - "Exit and clean up network" is the ONLY tray action that tears down routes/ICS.
    # If pystray/Pillow are unavailable the old behaviour degrades safely:
    # X = full teardown + exit, exactly like v1.4.92.
    ui_queue = queue.Queue()
    tray_icon_holder = {"icon": None}
    # v1.5.0 (P0): single teardown entry guard (see _teardown_and_exit).
    teardown_state = {"started": False}

    def _tray_image():
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(37, 99, 235, 255))
        draw.ellipse([24, 16, 40, 32], fill=(255, 255, 255, 255))
        draw.rectangle([18, 40, 46, 45], fill=(255, 255, 255, 255))
        return img

    def _tray_show(_icon=None, _item=None):
        ui_queue.put("SHOW")

    def _tray_hide(_icon=None, _item=None):
        ui_queue.put("HIDE")

    def _tray_exit(_icon=None, _item=None):
        ui_queue.put("QUIT")

    def _tray_settings(_icon=None, _item=None):
        # v1.5.44: tray grew to 4 items — Settings opens the new panel.
        ui_queue.put("SETTINGS")

    def _make_tray():
        try:
            import pystray
            Menu, MenuItem = pystray.Menu, pystray.MenuItem
            icon = pystray.Icon(
                "LinkHelm",
                _tray_image(),
                tr("LinkHelm Desktop (click to show)", "LinkHelm Desktop（点按显示窗口）"),
                Menu(
                    MenuItem(tr("Show Window", "显示窗口"), _tray_show, default=True),
                    MenuItem(tr("Hide to Tray", "隐藏到托盘"), _tray_hide),
                    MenuItem(tr("Settings", "设置"), _tray_settings),
                    Menu.SEPARATOR,
                    MenuItem(tr("Quit & Clean Up Network", "退出并清理网络"), _tray_exit),
                ),
            )
            threading.Thread(target=icon.run, daemon=True).start()
            tray_icon_holder["icon"] = icon
            return True
        except Exception:
            return False

    def _show_window():
        root.deiconify()
        root.lift()
        root.focus_force()

    def _teardown_and_exit():
        # Former on_closing body — the ONLY exit path that tears down the
        # network. Kept synchronous: the reap below VERIFIES death (v1.4.91).
        # v1.5.0 (P0): one-shot guard — a tray QUIT racing a window-close X
        # must never run the teardown twice (double taskkill / double ICS
        # rollback). Reset only on the failure path so the user can retry
        # after fixing the sharing cleanup.
        if teardown_state["started"]:
            return
        teardown_state["started"] = True
        if tray_icon_holder["icon"] is not None:
            try:
                tray_icon_holder["icon"].stop()
            except Exception:
                pass
        if app.tun_mgr or app.ics_mgr.needs_cleanup:
            print("Cleaning up before exit...")
            if not app._do_stop(is_closing=True):
                # v1.5.41: silent UX — keep the window open, explain in the
                # log/status instead of a modal dialog (user can retry).
                teardown_state["started"] = False
                app.log("Could not restore Windows sharing settings; stop sharing before closing the app.")
                root.after(0, lambda: app._set_status(tr("Status: Sharing cleanup failed, stop sharing first", "状态：共享清理失败，请先停止共享"), "error"))
                return
        # v1.4.63/91: TRUE exit — the VPN must never outlive the window; the
        # client itself force-exits after destroy so no half-alive teardown
        # state lingers.
        _kill_tun2socks_all()
        root.destroy()
        os._exit(0)

    def on_close_request():
        # v1.5.44: X ALWAYS minimizes to tray — never an implicit exit.
        # The tray menu's "Exit and clean up network" is the ONLY full-exit path, so
        # an idle-window close can never silently tear down the tunnel or
        # surprise the user. Without a tray (pystray unavailable) the old
        # safe degrade (full teardown + exit) is kept.
        if tray_icon_holder["icon"] is None:
            _teardown_and_exit()
            return
        app.log("Minimized to tray (the app keeps running); to fully quit, use the tray menu \u201cQuit & Clean Up Network\u201d.")
        root.withdraw()

    def _process_ui_queue():
        try:
            while True:
                action = ui_queue.get_nowait()
                if action == "SHOW":
                    _show_window()
                elif action == "HIDE":
                    root.withdraw()
                elif action == "SETTINGS":
                    # v1.5.44: tray "Settings" -> the new settings panel.
                    app.open_settings()
                elif action == "QUIT":
                    _teardown_and_exit()
                    return  # process is exiting; stop rescheduling
        except queue.Empty:
            pass
        root.after(200, _process_ui_queue)

    if _make_tray():
        app.log("Tray residency enabled: closing the window only minimizes to tray (the app keeps running); the tray menu opens Settings or quits fully.")
    root.protocol("WM_DELETE_WINDOW", on_close_request)
    _process_ui_queue()

    # v1.5.44: launched via the LoginStartup task → hide to tray immediately
    # (the window stays a control surface, nothing is torn down) and, only if
    # the user enabled "auto-connect" in Settings, run a BOUNDED connect worker:
    # poll the phone's SOCKS5 every 10s for up to ~120s, then start on the
    # GUI thread. Give-up is silent-in-tray, never an error dialog.
    if startup_hidden:
        root.withdraw()
        app.log("Silently entered tray standby via logon startup.")

        def _autoconnect_worker():
            settings = load_app_settings()
            if not settings.get("connectOnLaunch"):
                return
            try:
                ip = app.phone_ip.get().strip()
                port = int(app.phone_port.get().strip())
            except Exception:
                return
            settings_u = settings.get("socksUser") or DEFAULT_PHONE_USER
            settings_p = settings.get("socksPass") or DEFAULT_PHONE_PASS
            deadline = time.time() + 120
            while time.time() < deadline:
                # v1.5.47: compare against SOCKS5_OK explicitly (the string
                # return is always truthy — the bare `if` never probed) and
                # authenticate per the phone's userpass policy.
                if check_socks5(ip, port, timeout=2.0,
                                socks_user=settings_u,
                                socks_pass=settings_p) == "SOCKS5_OK":
                    def _go():
                        app.log("Auto-connect: phone proxy detected ready; starting connection.")
                        app.start()
                    app.root.after(0, _go)
                    return
                time.sleep(10)
            app.root.after(
                0, lambda: app.log("Auto-connect: timed out waiting for the phone proxy (120s); staying in tray standby.")
            )

        threading.Thread(target=_autoconnect_worker, daemon=True).start()

    root.mainloop()

if __name__ == "__main__":
    # v1.4.5: single-instance guard. Two live instances fight over Wintun,
    # the /1 routes and ICS until both break ("clash"). A named OS mutex makes
    # any second launch exit cleanly with a clear message instead. Checked
    # BEFORE the UAC relaunch so a duplicate start never prompts for admin.
    #
    # v1.4.62: the silent-elevation path (schtasks /run) can start the elevated
    # instance while the non-admin launcher is still shutting down — the mutex
    # is still held for a few hundred ms. Retry briefly instead of bouncing
    # the legitimate relaunch (user-visible as "double-click does nothing").
    # A genuinely live first instance holds the mutex for its whole lifetime,
    # so after ~3.5s of retries it really is a duplicate.
    startup_hidden = LOGIN_STARTUP_ARG in sys.argv
    if IS_WINDOWS:
        _granted = False
        try:
            for _ in range(7):
                ctypes.windll.kernel32.CreateMutexW(
                    None, False, "Global\\LinkHelm_SingleInstance"
                )
                if ctypes.windll.kernel32.GetLastError() != 183:  # not ERROR_ALREADY_EXISTS
                    _granted = True
                    break
                time.sleep(0.5)
            if not _granted:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "LinkHelm is already running (possibly minimized to the tray or taskbar).\n"
                    "Quit it from the tray menu via \u201cQuit & Clean Up Network\u201d before starting a new one.\n\n"
                    "Tip: running two copies at once makes them fight over routes and sharing settings, breaking networking on both.",
                    "LinkHelm",
                    0x00000040,  # MB_ICONINFORMATION
                )
                sys.exit(0)
        except Exception:
            pass  # mutex unavailable → fall back to legacy behaviour
    if IS_WINDOWS:
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                # v1.4.6: prefer SILENT elevation through the scheduled task
                # (registered with highest privileges on a previous elevated
                # run) — no UAC prompt on subsequent launches.
                if _launch_via_scheduled_task():
                    sys.exit(0)
                # Fallback: first-ever run (or task missing) → classic UAC.
                ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
                sys.exit(0)
        except Exception:
            pass
        # Elevated context: v1.5.44 — autostart is now USER-controlled, not
        # unconditional. (Re)register the silent-elevation task pointing at
        # the CURRENT binary so upgrades never launch a stale version; the
        # logon task only exists while the user enabled it in Settings.
        _ensure_elevation_task()
        _app_settings = load_app_settings()
        _sync_login_startup(bool(_app_settings.get("startAtLogon")))

    # Apply the saved UI language (default English) before building any UI.
    try:
        _init_settings = load_app_settings()
        LANG = _init_settings.get("language", "en") or "en"
    except Exception:
        LANG = "en"

    main(startup_hidden=startup_hidden)