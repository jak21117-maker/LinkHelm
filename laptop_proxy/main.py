import customtkinter as ctk
from tkinter import messagebox
from tkinter import filedialog
import atexit
import traceback
import file_transfer
import store_accel
import relay
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

# the user's personal panel token — shipping it (or leaving it in the source)
# leaks their account, so the box starts empty and the user pastes their own
# link. A previously used link is still restored at runtime from accel.json.
DEFAULT_STORE_SUB_URL = ""
import webbrowser
from console_bridge import BridgeError, IcsManager
# import symbols

# --- Windows specific imports ---
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import winreg
    import ctypes

# dying after Windows entered Modern Standby / idle sleep (the user reports
# "a process mounted too long gets killed by the Windows power policy" — the OS suspends the process tree
# and the Wi-Fi Direct link, the engine freezes, the tunnel looks dead).
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
# v1.5.47 — the lwIP engine always dials the phone's 8080 lane directly, with the
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
# 承接。以下仅保留版本说明；原生二进制随安装包 engine/ 目录提供，不再运行时下载。
WINTUN_VERSION = "0.14.1"
LWIP_ENGINE_VERSION = "2.19.1"  # hev-socks5-tunnel，见 DEPENDENCY_LOCK.md
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# The virtual adapter created by this program always uses this gateway, so route
# cleanup can target only routes that belong to LaptopProxy.
OUR_TUN_GATEWAY = "10.0.0.1"
TUN_LOCAL_IP = "10.0.0.2"# network flyout and ncpa.cpl show the connection name, and every netsh
# config call addresses the adapter by it — a drifted name ("LinkHelm 31"
# in the field) made those calls silent no-ops: no IP/DNS on the tunnel,
# ERR_NAME_NOT_RESOLVED pages, and a confusing duplicate network entry.
# _normalize_adapter_connection_name renames it back at every start.
TUN_ADAPTER_NAME = "LinkHelm-IwIP"

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
        # randomized). Persisted only after the user edits them in Settings.
        "socksUser": DEFAULT_PHONE_USER, "socksPass": DEFAULT_PHONE_PASS,
        # dialog opens and remembered across runs ("" = not set yet).
        "recvDir": "",
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

def _resolve_tun_connection_name(timeout=15):
    """beta1.0.51: resolve the ACTUAL connection name of our tunnel adapter.

    hev-socks5-tunnel reopens the adapter by its fixed GUID, and Windows does
    NOT rename an existing adapter — so on machines where the connection name
    drifted (the field "LinkHelm 31" flyout entry), every netsh
    `name=LinkHelm-IwIP` call matched nothing: the IP/DNS configuration never
    applied, the tunnel came up without a resolver, and pages failed with
    ERR_NAME_NOT_RESOLVED (computer-manager DNS scans flag it as 异常).

    Finds the adapter by its own IP (10.0.0.1 / 10.0.0.2) instead of by name,
    because `netsh interface ipv4 show addresses` prints the name in quotes
    per interface block and the IP literals are locale-independent. Returns
    the current name, or None when the adapter is not up (yet).
    """
    if not IS_WINDOWS:
        return None
    try:
        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "addresses"], timeout=timeout
        )
    except Exception:
        return None
    current = None
    for line in (out or "").splitlines():
        quoted = re.search(r'"([^"]+)"', line)
        if quoted and ("interface" in line.lower() or "接口" in line):
            current = quoted.group(1)
            continue
        if current and ("10.0.0.1" in line or "10.0.0.2" in line):
            return current
    return None

def _cleanup_stale_tun_adapters(log_fn):
    """beta1.0.5: remove LEGACY LinkHelm*/LaptopProxyVPN* Wintun adapters,
    always PRESERVING the current LinkHelm-IwIP adapter.

    A crashed/force-killed run (and the old tun2socks engine) leaves its Wintun
    adapter behind; every new run would otherwise collide with a stale name and
    Windows would append "LinkHelm 2/3/4...". beta1.0.5 names the adapter
    LinkHelm-IwIP (fixed GUID — see engine.ADAPTER_NAME / engine.ADAPTER_GUID),
    so the sweep targets ONLY the old names and never touches the live one:
      - first launch  : legacy LinkHelm / LinkHelm N / LaptopProxyVPN* are
                        removed (incl. tun2socks leftovers), hev then creates
                        LinkHelm-IwIP fresh;
      - later launches: only LinkHelm-IwIP exists; it is excluded from the sweep
                        and reused as-is — WintunCreateAdapter reopens the same
                        adapter by GUID, so no "LinkHelm-IwIP 2/3/4" appears.
    This is the "先清理旧的 → 建立新的 → 后续复用" lifecycle; the distinct name
    is what makes "旧的清理不掉、新的也用不了" impossible.

    Sweeps three layers: PnP devices, NetCfg connection-name entries (the real
    source of the climbing number), and NLA network profiles. Best-effort:
    failures are ignored so startup is never blocked by hygiene.

    beta1.0.51: the NetCfg layer now ALSO excludes our adapter by its fixed
    GUID (the registry key IS the GUID), not just by its canonical name — a
    drifted connection name ("LinkHelm 31") must never make the sweep delete
    the LIVE tunnel adapter.
    """
    if not IS_WINDOWS:
        return
    try:
        adapter_guid = None
        try:
            from engine import ADAPTER_GUID
            adapter_guid = ADAPTER_GUID
        except Exception:
            adapter_guid = None
        guid_guard = (
            f" -and $_.PSChildName -ne '{adapter_guid}'" if adapter_guid else ""
        )
        ps = (
            # Remove every legacy LinkHelm*/LaptopProxyVPN* PnP device (incl.
            # tun2socks random-GUID leftovers) EXCEPT the live LinkHelm-IwIP.
            "Get-PnpDevice -FriendlyName 'LinkHelm*','LaptopProxyVPN*' | "
            "Where-Object { $_.FriendlyName -ne 'LinkHelm-IwIP' } | "
            "ForEach-Object { pnputil /remove-device $_.InstanceId } | Out-Null; "
            # NetCfg connection entries — the numbering source. The registry
            # key is the adapter GUID, so the fixed-GUID adapter is excluded
            # even when its display name has drifted.
            "Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Network"
            "\\{4D36E972-E325-11CE-BFC1-08002BE10318}' -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "$n = (Get-ItemProperty -Path ($_.PSPath + '\\Connection') "
            "-Name Name -ErrorAction SilentlyContinue).Name; "
            "($n -like 'LinkHelm*' -or $n -like 'LaptopProxyVPN*') "
            "-and $n -ne 'LinkHelm-IwIP'" + guid_guard + " } | "
            "Remove-Item -Recurse -ErrorAction SilentlyContinue; "
            # NLA network profiles.
            "Get-ChildItem 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
            "\\Network\\Profiles' -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "($_.GetValue('ProfileName') -like 'LinkHelm*' -or "
            "$_.GetValue('ProfileName') -like 'LaptopProxyVPN*') -and "
            "($_.GetValue('ProfileName') -ne 'LinkHelm-IwIP') } | "
            "Remove-Item -Recurse -ErrorAction SilentlyContinue"
        )
        _run_decoded(
            ["powershell", "-NoProfile", "-Command", ps], timeout=60
        )
        log_fn("Cleaned up legacy leftover virtual NICs (kept LinkHelm-IwIP for reuse).")
    except Exception:
        pass  # best effort — never block startup on hygiene

def _cleanup_stale_wlan_profiles(log_fn):
    """beta1.0.52: drop numbered duplicate WLAN profiles ("LinkHelm 2".."LinkHelm 31").

    beta1.0.64: CALLED AGAIN at startup, with a second guard. Every Wi-Fi
    Direct / hotspot session makes Windows save ANOTHER numbered profile
    ("LinkHelm 2" ... "LinkHelm 31"), and each stale one keeps showing up in
    the network flyout as "无法访问 Internet" forever (see the reported
    screenshot). It was disabled in beta1.0.55 because deleting the profile
    of the network Windows is currently associated with drops the link; the
    guard below now skips BOTH the exact active profile/SSID and its base
    name, so an in-use "LinkHelm 31" can never be deleted even when the
    interface only reports the SSID "LinkHelm".
    """
    if not IS_WINDOWS:
        return
    try:
        # delete the profile of the network it is currently using, so collect
        # every profile/SSID that is live on any WLAN interface and never
        # touch those. (zh-CN output uses "配置文件", en-US uses "Profile".)
        active = set()
        _, iface_out, _ = _run_decoded(
            ["netsh", "wlan", "show", "interfaces"], timeout=15
        )
        for line in (iface_out or "").splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip().lower() in ("ssid", "profile", "配置文件"):
                v = val.strip()
                if v:
                    active.add(v)

        # being auto-connected — the sweep must never race the association it
        # is supposed to protect.
        # "LinkHelm-5", "linkhelm 12" ... every hotspot/Wi-Fi-Direct session
        # can mint another numbered clone).
        if any(a.lower().strip().startswith(("linkhelm", "direct-linkhelm"))
               for a in active):
            return

        _, out, _ = _run_decoded(["netsh", "wlan", "show", "profiles"], timeout=15)
        stale = set()
        for line in (out or "").splitlines():
            name = line.split(":", 1)[1].strip() if ":" in line else ""
            # beta1.0.7 NAME-DRIFT sweep: delete every drifted clone —
            # "(DIRECT-)LinkHelm" followed by ANY suffix ("LinkHelm 31",
            # "LinkHelm-7", case-insensitive). The bare base names
            # ("LinkHelm", "DIRECT-LinkHelm") are KEPT: auto-reconnect to
            # the phone uses them, deleting those is what broke 1.0.55.
            low = name.lower().strip()
            if low in ("linkhelm", "direct-linkhelm") or not low:
                continue
            if low.startswith(("linkhelm", "direct-linkhelm")):
                stale.add(name)
        # live ("LinkHelm 31" while the interface reports SSID "LinkHelm")
        # must survive, otherwise the association drops — the beta1.0.55
        # "电脑会断开 WiFi" report. beta1.0.7: base-normalise the drift too
        # (any "…linkhelm…" name collapses to its family root).
        def _base(n):
            low = n.lower().strip()
            if low.startswith("direct-linkhelm"):
                return "direct-linkhelm"
            if low.startswith("linkhelm"):
                return "linkhelm"
            return low

        live_bases = {_base(a) for a in active}
        stale = {n for n in stale if n not in active and _base(n) not in live_bases}
        for name in sorted(stale):
            _run_decoded(
                ["netsh", "wlan", "delete", "profile", f"name={name}"], timeout=15
            )
        if stale:
            log_fn(
                f"Removed {len(stale)} stale duplicate WLAN profile(s): {', '.join(sorted(stale))}."
            )
    except Exception:
        pass  # best effort — never block startup on hygiene

# ---------------------------------------------------------------------------
#
# The network flyout and the Network & Internet settings page show EVERY
# LinkHelm network — the Wi-Fi Direct group and the tunnel adapter — as
# "No Internet", even while pages load fine. That label is produced by NCSI
# (Network Connectivity Status Indicator), and it is not cosmetic: a large
# number of desktop apps (Store/UWP, launchers, game clients) ask Windows
# "do I have internet?" before they will start, which is the reported "网页能
# 打开，但应用打不开".
#
# NCSI decides by actively probing Microsoft-owned endpoints
# (dns.msftncsi.com / www.msftncsi.com). Those are unreachable or very slow on
# most domestic networks and are additionally forced through the tunnel, so the
# probe times out and Windows labels a perfectly working link as offline.
#
# Fix: pause NCSI active probing for the duration of the session (the standard
# corporate workaround) and restore the original value on disconnect. With
# active probing off, NLA reports the link that owns the routes as
# Internet-connected. The change is one DWORD, is reverted on every exit path,
# and never touches Wi-Fi or routing.
# ---------------------------------------------------------------------------
NCSI_REG_KEY = r"HKLM\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet"
NCSI_PROBE_VALUE = "EnableActiveProbing"

# beta1.0.82+ (settled): NCSI stays at the MICROSOFT DEFAULTS. The old
# 1.0.71..1.0.81 "probe Baidu instead" idea was disproven live — with the
# tunnel up, Microsoft's endpoints are reachable through the phone upstream,
# and the flyout showed "Internet" the moment the defaults were restored.
# _set_domestic_ncsi_probe() therefore only HEALS leftovers back to defaults.
NCSI_PROBE_VALUES = ("ActiveWebProbeHost", "ActiveWebProbePath",
                     "ActiveWebProbeContent",
                     "ActiveWebProbeContentMatch",
                     "ActiveWebProbeHostV6", "ActiveWebProbePathV6",
                     "ActiveWebProbeContentV6")
NCSI_MICROSOFT_DEFAULT = {
    "ActiveWebProbeHost": "www.msftconnecttest.com",
    "ActiveWebProbePath": "/connecttest.txt",
    "ActiveWebProbeContent": "Microsoft Connect Test",
    "ActiveWebProbeContentMatch": "Microsoft Connect Test",
    "ActiveWebProbeHostV6": "ipv6.msftconnecttest.com",
    "ActiveWebProbePathV6": "/connecttest.txt",
    "ActiveWebProbeContentV6": "Microsoft Connect Test",
}

def _reg_key_writable(root, path):
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False

def _set_domestic_ncsi_probe(log_fn=lambda _m: None):
    """beta1.0.82 (semantics changed): heal NCSI back to Microsoft defaults.

    Older builds (1.0.71..1.0.81) pointed the probe at Baidu; the redirect is
    now known to be wrong (see the history block above). This overwrites
    every probe value with the Microsoft default — leftover Baidu values from
    a crashed older session get cleaned up too. No backup is taken because we
    never write anything but the Microsoft defaults; there is nothing
    session-specific to restore.
    Returns True when the key was writable (elevated session).
    """
    if not IS_WINDOWS:
        return False
    global _ncsi_probe_backup
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet",
            0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        log_fn("NOTE: NCSI probe values left untouched (needs administrator "
               "rights).")
        return False
    changed = False
    try:
        for name in NCSI_PROBE_VALUES:
            target = NCSI_MICROSOFT_DEFAULT[name]
            try:
                current, _ = winreg.QueryValueEx(key, name)
            except OSError:
                current = None
            if current != target:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, target)
                changed = True
    finally:
        winreg.CloseKey(key)
    _ncsi_probe_backup = None  # nothing session-specific to restore
    if changed:
        log_fn("[ncsi] probe values healed to Microsoft defaults (cleaned "
               "any leftover Baidu redirect from 1.0.71..1.0.81).")
        # Restart NlaSvc so the cached verdict is dropped and the next probe
        # runs immediately with the healed values (beta1.0.82).
        _restart_nlasvc(log_fn)
    else:
        log_fn("[ncsi] probe values already at Microsoft defaults.")
    return True

def _restart_nlasvc(log_fn=lambda _m: None):
    """beta1.0.81: force NCSI to re-probe with the freshly written values.

    Only called after a successful elevated registry write. Without the
    restart Windows may keep serving the cached captive-portal verdict for
    the LinkHelm-IwIP network until an arbitrary future re-probe.
    """
    if not IS_WINDOWS:
        return
    import subprocess
    try:
        stop = subprocess.run(["net", "stop", "nlasvc", "/y"],
                              capture_output=True, timeout=30)
        log_fn("[ncsi] net stop nlasvc -> rc=%d %s" % (
            stop.returncode,
            (stop.stderr or stop.stdout or b"").decode("utf-8", "replace").strip()))
    except Exception as exc:
        log_fn("[ncsi] net stop nlasvc failed: %s" % exc)
    try:
        start = subprocess.run(["net", "start", "nlasvc"],
                               capture_output=True, timeout=30)
        log_fn("[ncsi] net start nlasvc -> rc=%d %s" % (
            start.returncode,
            (start.stderr or start.stdout or b"").decode("utf-8", "replace").strip()))
    except Exception as exc:
        log_fn("[ncsi] net start nlasvc failed: %s" % exc)

def _restore_ncsi_probe(log_fn=lambda _m: None):
    """beta1.0.82: nothing to restore — probe values stay at Microsoft
    defaults for the whole session (kept as a no-op for the exit paths that
    still call it, so old call sites don't need rewriting)."""
    return False

_ncsi_probe_backup = None

# Crashed beta1.0.55/1.0.71 sessions left EnableActiveProbing=0 behind; the
# network flyout then showed "无法访问 Internet" for every network even before
# the user ever connected (re-confirmed live on 1.0.72). The original value
# is restored at process exit.
_startup_ncsi_heal = None

def _atexit_restore_startup_heal():
    global _startup_ncsi_heal
    if _startup_ncsi_heal is None:
        return
    val, _startup_ncsi_heal = _startup_ncsi_heal, None
    try:
        _set_ncsi_probing(bool(val))
    except Exception:
        pass
    try:
        _restore_ncsi_probe()
    except Exception:
        pass

def _heal_ncsi_at_startup(log_fn=lambda _m: None):
    global _startup_ncsi_heal
    if not IS_WINDOWS:
        return
    present, previous = _read_ncsi_probing()
    _startup_ncsi_heal = previous if present else 1
    if _startup_ncsi_heal != 1:
        if _set_ncsi_probing(True, log_fn):
            log_fn("[ncsi] healed a stale disabled probe state at launch "
                   "(left by an older build); restored at exit.")
    _set_domestic_ncsi_probe(log_fn)
    atexit.register(_atexit_restore_startup_heal)

# ---------------------------------------------------------------------------
#
# Get-NetConnectionProfile shows it plainly: the app's own lwIP tunnel
# adapter ("LinkHelm-IwIP") gets a NEW net-connection-profile NAME on every
# session ("LinkHelm 2", "LinkHelm 3", ... "LinkHelm 31"). Windows lists each
# profile in the network flyout, NCSI can't probe it, so every ghost shows
# "无法访问 Internet" forever. Cleaning WLAN profiles (1.0.63..1.0.7) treated
# the wrong layer — the fix lives in the NetworkList (INetworkListManager):
# delete every DISCONNECTED "LinkHelm*" network, and rename the connected one
# to the fixed name "LinkHelm" so the drift can never start again.
# ---------------------------------------------------------------------------
_NLM_CLSID = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
_NLM_IID = "{DCB00000-570F-4A9B-8D69-199FDBA5723B}"
_ENUM_IID = "{DCB00002-570F-4A9B-8D69-199FDBA5723B}"
_NET_IID = "{DCB00001-570F-4A9B-8D69-199FDBA5723B}"

def _nlm_collect():
    """(release_fn, [(name, connected, net_ptr)]) via raw COM vtables."""
    import ctypes
    from ctypes import c_void_p, c_ulong, c_int16, POINTER, byref, cast

    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx(None, 0)

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                    ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def mk(s):
        import uuid
        u = uuid.UUID(s)
        b = u.bytes_le
        g = GUID()
        g.d1 = int.from_bytes(b[0:4], "little")
        g.d2 = int.from_bytes(b[4:6], "little")
        g.d3 = int.from_bytes(b[6:8], "little")
        g.d4 = (ctypes.c_ubyte * 8)(*b[8:16])
        return g

    clsid = mk(_NLM_CLSID)
    iid = mk(_NLM_IID)
    enum_iid = mk(_ENUM_IID)
    net_iid = mk(_NET_IID)

    p = c_void_p()
    hr = ole32.CoCreateInstance(byref(clsid), None, 1,  # CLSCTX_INPROC_SERVER
                                byref(iid), byref(p))
    if hr != 0 or not p:
        raise OSError("CoCreateInstance(NetworkListManager) 0x%08X" % hr)

    vtbl = cast(p, POINTER(c_void_p)).contents.value
    table = ctypes.cast(vtbl, POINTER(c_void_p))

    # slot 7: GetNetworks(flags, &enum)
    get_nets = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_ulong,
                                  POINTER(c_void_p))(table[7])
    enum_p = c_void_p()
    hr = get_nets(p, 3, byref(enum_p))  # CONNECTED|DISCONNECTED
    if hr != 0 or not enum_p:
        raise OSError("GetNetworks 0x%08X" % hr)

    e_vtbl = cast(enum_p, POINTER(c_void_p)).contents.value
    e_table = ctypes.cast(e_vtbl, POINTER(c_void_p))
    # slot 8: Next(celt, rgelt, &fetched) — IEnumNetworks : IDispatch,
    # get__NewEnum sits at 7, Next at 8 (verified against netlistmgr.h).
    next_fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_ulong,
                                 POINTER(c_void_p), POINTER(c_ulong))(e_table[8])

    out = []
    while True:
        one = c_void_p()
        fetched = c_ulong(0)
        hr = next_fn(enum_p, 1, byref(one), byref(fetched))
        if hr != 0 or fetched.value == 0 or not one:
            break
        n_vtbl = cast(one, POINTER(c_void_p)).contents.value
        n_table = ctypes.cast(n_vtbl, POINTER(c_void_p))
        # slot 7 GetName(BSTR*) ; slot 15 IsConnected(VARIANT_BOOL*)
        get_name = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p,
                                      POINTER(c_void_p))(n_table[7])
        is_conn = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p,
                                     POINTER(c_int16))(n_table[16])
        bstr = c_void_p()
        conn = c_int16(0)
        name = ""
        if get_name(one, byref(bstr)) == 0 and bstr.value:
            s = ctypes.windll.oleaut32.SysStringLen(bstr)
            name = ctypes.wstring_at(bstr.value, s)
            ctypes.windll.oleaut32.SysFreeString(bstr)
        is_conn(one, byref(conn))
        out.append((name, bool(conn), one))

    def _slot(vtbl_addr, idx):
        tbl = ctypes.cast(vtbl_addr, POINTER(c_void_p))
        return tbl[idx]

    def _release_if(p):
        if not p:
            return
        release_fn = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(_slot(
            ctypes.cast(p, POINTER(c_void_p)).contents.value, 2))
        release_fn(p)

    def _release():
        for _n, _c, ptr in out:
            _release_if(ptr)
        _release_if(enum_p)
        _release_if(p)
        ole32.CoUninitialize()

    return _release, out

def sweep_ghost_linkhelm_networks(log_fn=lambda _m: None):
    """Delete drifted "LinkHelm 2..N" ghost network-list entries.

    beta1.0.71, verified live: the flyout ghosts are net-connection-profiles
    of the app's own lwIP adapter ("LinkHelm-IwIP") — one new drifted name
    ("LinkHelm 2" .. "LinkHelm 37") per session, each showing
    "无法访问 Internet" forever. Two fixes, both needed:

    1. the CONNECTED session's profile is RENAMED to the fixed "LinkHelm"
       (INetwork::SetName via IDispatch) so the drift can never start again;
    2. DISCONNECTED ghosts are deleted from the NetworkList store
       (HKLM/.../NetworkList "Profiles" + "Signatures") — the real INetwork
       interface has no Delete method (verified live: DISP_E_UNKNOWNNAME),
       so the registry store is the only way. Needs administrator rights,
       which the app already has for its routes.

    "DIRECT-LinkHelm" (the phone's Wi-Fi Direct network) is NEVER touched.
    Returns the count of deleted ghosts; errors are logged, never raised.
    """
    if not IS_WINDOWS:
        return 0
    # ---- pass 1: COM enumeration — who is connected? -------------------
    try:
        release, nets = _nlm_collect()
    except Exception as exc:
        log_fn("[netlist] unavailable (%s) — ghost entries untouched" % exc)
        return 0
    ghosts, live_drift = set(), None
    try:
        for name, connected, _ptr in nets:
            low = (name or "").strip().lower()
            if low == "direct-linkhelm":
                continue  # the phone's Wi-Fi Direct network — untouchable
            if not (low == "linkhelm" or low.startswith("linkhelm ")):
                continue
            if connected:
                if low != "linkhelm":
                    live_drift = name
            else:
                ghosts.add(name)
    finally:
        try:
            release()
        except Exception:
            pass

    # ---- pass 2: rename the live drift so no NEW numbers appear ---------
    renamed = 0
    if live_drift:
        try:
            release2, nets2 = _nlm_collect()
            try:
                import ctypes
                from ctypes import c_void_p, c_long, c_uint, c_ushort, \
                    c_ulong, POINTER
                GUID_NULL = ctypes.create_string_buffer(16)
                SysAllocString = ctypes.windll.oleaut32.SysAllocString
                SysAllocString.restype = c_void_p

                def vtbl(p, idx):
                    return ctypes.cast(ctypes.cast(p, POINTER(c_void_p)).contents,
                                       POINTER(c_void_p))[idx]

                for name, connected, ptr in nets2:
                    if name != live_drift or not connected:
                        continue
                    # IDispatch::GetIDsOfNames("SetName") -> dispid
                    names = (c_void_p * 1)(SysAllocString("SetName"))
                    disp = (c_long * 1)()
                    get_ids = ctypes.WINFUNCTYPE(
                        ctypes.HRESULT, c_void_p, c_void_p, c_void_p,
                        c_uint, c_ulong, POINTER(c_long))(vtbl(ptr, 5))
                    hr = get_ids(ptr, ctypes.byref(GUID_NULL), names,
                                 1, 0x0409, disp)
                    if hr != 0:
                        break
                    # VARIANT {vt=VT_BSTR, bstrVal="LinkHelm"}
                    var = (c_ushort * 4)()
                    ctypes.cast(var, POINTER(c_ushort))[0] = 8  # VT_BSTR
                    ctypes.cast(var, POINTER(c_void_p))[1] = \
                        SysAllocString("LinkHelm")
                    # DISPPARAMS{rgvarg=&var, rgdispidNamedArgs=None,
                    #            cArgs=1, cNamedArgs=0}
                    args_buf = (c_void_p * 4)()
                    args_buf[0] = ctypes.addressof(var)
                    args_buf[2] = 1  # cArgs
                    inv = ctypes.WINFUNCTYPE(
                        ctypes.HRESULT, c_void_p, c_long, c_void_p,
                        c_ulong, c_ushort, c_void_p, c_void_p,
                        c_void_p, c_void_p)(vtbl(ptr, 6))
                    try:
                        hr = inv(ptr, disp[0], ctypes.byref(GUID_NULL),
                                 0x0409, 1, ctypes.addressof(args_buf),
                                 None, None, None)
                    except OSError as exc:
                        # DISP_E_EXCEPTION (0x80020009) from NLM = the caller
                        # lacks the rights to rename the connection profile.
                        if getattr(exc, "winerror", None) == -2147352567:
                            log_fn("[netlist] rename of \"%s\" rejected by "
                                   "Windows — needs administrator rights"
                                   % live_drift)
                            break
                        raise
                    if hr == 0:
                        renamed = 1
                        log_fn("[netlist] renamed \"%s\" -> "
                               "\"LinkHelm\" (fixed name, no more "
                               "drift)" % live_drift)
                    else:
                        log_fn("[netlist] rename failed 0x%08X "
                               "(needs administrator rights)" % hr)
                    break
            finally:
                release2()
        except Exception as exc:
            log_fn("[netlist] rename of \"%s\" failed: %s" % (live_drift, exc))

    # ---- pass 3: delete disconnected ghosts from the NetworkList store --
    deleted = 0
    if not ghosts:
        return renamed
    try:
        import winreg
        base = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
                r"\NetworkList")
        targets = {g.lower() for g in ghosts}

        def _match(name_val):
            return (name_val or "").strip().lower() in targets

        def _purge(root_key, path):
            n = 0
            try:
                key = winreg.OpenKey(root_key, path, 0,
                                     winreg.KEY_READ | winreg.KEY_SET_VALUE)
            except OSError:
                return 0
            try:
                subs = []
                i = 0
                while True:
                    try:
                        subs.append(winreg.EnumKey(key, i)); i += 1
                    except OSError:
                        break
                for sub in subs:
                    sub_path = path + "\\" + sub
                    try:
                        sk = winreg.OpenKey(key, sub)
                    except OSError:
                        continue
                    name_val = None
                    for vn in ("ProfileName", "Name"):
                        try:
                            name_val, _ = winreg.QueryValueEx(sk, vn)
                            break
                        except OSError:
                            continue
                    winreg.CloseKey(sk)
                    if _match(name_val):
                        try:
                            winreg.DeleteKey(key, sub)
                            n += 1
                            continue
                        except OSError:
                            pass
                    n += _purge(root_key, sub_path)
            finally:
                winreg.CloseKey(key)
            return n

        deleted += _purge(winreg.HKEY_LOCAL_MACHINE, base + r"\Profiles")
        deleted += _purge(winreg.HKEY_LOCAL_MACHINE, base + r"\Signatures")
    except Exception as exc:
        log_fn("[netlist] registry cleanup failed (%s) — needs "
               "administrator rights" % exc)
        return renamed
    if deleted:
        log_fn("[netlist] removed %d ghost network(s): %s"
               % (deleted, ", ".join(sorted(ghosts))))
    elif ghosts:
        log_fn("[netlist] %d ghost(s) found (%s) but none removable — "
               "needs administrator rights" % (len(ghosts),
                                               ", ".join(sorted(ghosts))))
    return deleted

def _read_ncsi_probing():
    """beta1.0.55: current EnableActiveProbing state as (present, value)."""
    if not IS_WINDOWS:
        return (False, None)
    try:
        rc, out, _ = _run_decoded(
            ["reg", "query", NCSI_REG_KEY, "/v", NCSI_PROBE_VALUE], timeout=15
        )
        if rc != 0 or not out:
            return (False, None)
        match = re.search(
            r"EnableActiveProbing\s+REG_DWORD\s+(0x[0-9a-fA-F]+|\d+)", out or ""
        )
        if not match:
            return (False, None)
        raw = match.group(1)
        return (True, int(raw, 16) if raw.lower().startswith("0x") else int(raw))
    except Exception:
        # Best-effort hygiene only: a locked-down registry (or a blocked reg.exe)
        # must never influence startup. "Not present" → caller uses the default.
        return (False, None)

def _set_ncsi_probing(enabled, log_fn=lambda _m: None):
    """beta1.0.55: enable/disable NCSI active probing. Returns True on success."""
    if not IS_WINDOWS:
        return False
    try:
        rc, _, _ = _run_decoded(
            ["reg", "add", NCSI_REG_KEY, "/v", NCSI_PROBE_VALUE,
             "/t", "REG_DWORD", "/d", "1" if enabled else "0", "/f"],
            timeout=20,
        )
    except Exception:
        rc = 1
    if rc != 0:
        log_fn(
            "NOTE: could not change the Windows connectivity-probe setting "
            "(needs administrator rights). The network list may still show "
            "\"No Internet\" — the connection itself is unaffected."
        )
        return False
    return True

# but a hard kill (Task Manager, power loss) would leave NCSI probing disabled
# for good. atexit covers the ordinary "close the window / Ctrl-C" cases.
_ACTIVE_TUN_MANAGER = None

def _atexit_restore_ncsi():
    manager = _ACTIVE_TUN_MANAGER
    if manager is None:
        return
    try:
        manager._restore_ncsi_probing()
    except Exception:
        pass

atexit.register(_atexit_restore_ncsi)

def _thread_excepthook(args):
    """beta1.0.55: never let a worker-thread exception vanish silently.

    The route sweep, the health monitor, the UDP/TCP probes and the tray
    poller all run on their own threads. Before this hook, an exception in any
    of them died with nothing but a bare interpreter line on stderr —
    invisible in a --windowed build — and the user just saw a feature stop
    working. Now every one is written to the console with the thread name so a
    report can be acted on. Deliberately never re-raises: a crashed hygiene
    thread must not take the GUI down with it.
    """
    try:
        name = getattr(args.thread, "name", "?")
        print(
            f"[thread {name}] {args.exc_type.__name__}: {args.exc_value}",
            file=sys.stderr,
        )
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass

threading.excepthook = _thread_excepthook

def _route_gateways(dest, mask):
    """Route lookup — list of gateways serving dest/mask.

    beta1.0.84: routed through the C++ netops module; the legacy Python
    `route print` parser below is the fallback for old packages.
    """
    try:
        import cpp_bridge
        if cpp_bridge.netops_available():
            return cpp_bridge.route_gateways(dest, mask)
    except Exception:
        pass
    try:
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

def _route_bindings(dest, mask):
    """beta1.0.72: (gateway, interface_ip) pairs serving dest/mask.

    `route print -4` prints the INTERFACE column as the interface's own IP
    address (not the ifIndex). The TUN interface is the one holding 10.0.0.2,
    so a /1 route bound to e.g. 192.168.49.74 (the WLAN) is visibly mis-bound.
    Live evidence (beta1.0.71 machine): `route add 0.0.0.0 mask 128.0.0.0
    10.0.0.1` bound the /1 route to the WLAN interface instead of the TUN —
    Windows resolved the gateway against whatever interface had a path to
    10.0.0.1 at that moment. Half the IP space then rides a route whose
    gateway is only properly reachable through the other adapter:
    intermittent, destination-dependent timeouts ("连通性很差").
    """
    try:
        _, out, _ = _run_decoded(["route", "print", "-4"], timeout=15)
    except Exception:
        return []
    if not out:
        return []
    pairs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == dest and parts[1] == mask:
            # interface column is an IPv4 dotted quad; skip anything else
            if parts[3].count(".") == 3:
                pairs.append((parts[2], parts[3]))
    return pairs

def _tun_interface_index(name=None):
    """Interface index of the LinkHelm-IwIP adapter (0 if unknown).

    beta1.0.84: resolved via the C++ netops module (GBK-safe); the Python
    netsh parser below is the fallback for old packages. The TUN adapter is
    persistent (fixed GUID), so its index survives across sessions.
    """
    try:
        import cpp_bridge
        if cpp_bridge.netops_available():
            idx = cpp_bridge.if_index(name or TUN_ADAPTER_NAME)
            if idx:
                return idx
    except Exception:
        pass
    try:
        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "interfaces"], timeout=15
        )
    except Exception:
        return 0
    want = (name or TUN_ADAPTER_NAME).strip().lower()
    for line in (out or "").splitlines():
        parts = line.split()
        # columns: Idx Met MTU State Name
        if len(parts) >= 5 and parts[-1].lower() == want:
            try:
                return int(parts[0])
            except ValueError:
                continue
    return 0

def _sweep_stale_tunnel_routes(log_fn):
    """beta1.0.52: delete stale routes pointing at our TUN gateway (10.0.0.1).

    A crashed/force-killed previous run — or any pre-beta1.0.52 build, whose
    `netsh set address ... <gateway>` side effect installed an automatic
    DEFAULT route — leaves 0.0.0.0/0 (and possibly the /1 pair) pointing at
    10.0.0.1 after the engine is gone. With metric 1 those beats the WLAN
    default route and the whole PC loses internet ("检测时电脑断网"). Called
    at App launch (elevated) so the machine self-heals even before the user
    connects; start() re-adds the tracked routes right after when sharing
    begins. Never touches other software's routes.
    """
    if not IS_WINDOWS:
        return
    try:
        deleted = []
        for dest, mask in (("0.0.0.0", "0.0.0.0"), ("0.0.0.0", "128.0.0.0"),
                           ("128.0.0.0", "128.0.0.0")):
            if OUR_TUN_GATEWAY in _route_gateways(dest, mask):
                subprocess.run(
                    ["route", "delete", dest, "mask", mask, OUR_TUN_GATEWAY],
                    capture_output=True,
                )
                deleted.append(f"{dest}/{mask}")
        if deleted:
            log_fn(
                "Removed stale tunnel routes via 10.0.0.1 (left behind by a crashed run): "
                + ", ".join(deleted)
                + ". Network connectivity restored."
            )
    except Exception:
        pass  # best effort — never block startup on hygiene

def _find_foreign_engine_procs():
    """beta1.0.5: image names of any hev-socks5-tunnel process already running.

    Old pre-lwIP runs (and a crashed beta1.0.5 run) can leave the native engine
    process alive. The only reliable cross-version signal is the engine process
    itself. Both the full image name and its 15-char truncated form are probed
    (some Windows builds truncate long image names). Returns an empty tuple when
    none is running; errors degrade to empty (never block start).
    """
    if not IS_WINDOWS:
        return ()
    try:
        _, out, _ = _run_decoded(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-Process hev-socks5-tunnel, hev-socks5-tun"
                " -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty ProcessName",
            ],
            timeout=15,
        )
        return tuple(
            line.strip().lower()
            for line in (out or "").splitlines()
            if line.strip().lower().startswith("hev-socks5-tun")
        )
    except Exception:
        return ()

def _kill_engine_procs_all(log_fn=None):
    """beta1.0.5: force-kill EVERY hev-socks5-tunnel process and its children.

    Two call sites share one contract — "the software owns the whole VPN
    lifecycle, nothing engine-related outlives it":
    - TunManager.start(): reconnect takes over. A leftover process (crashed
      run, or one started by an old build) would keep the old tunnel alive and
      its adapter stuck; instead of refusing, the new run kills it and proceeds.
    - App window close: TRUE exit. Even if graceful teardown fails, no engine
      child is allowed to outlive the window.
    taskkill /T /F also reaps child processes of the tree. Returns the image
    names that were seen before the kill (may be empty).
    """
    names = _find_foreign_engine_procs()
    if not names:
        return ()
    if log_fn:
        log_fn(f"Force-terminating leftover engine processes ({', '.join(names)})...")
    # taskkill /T /F kills the whole tree, Stop-Process is the fallback;
    # run both, both are best-effort. /IM matches the full 18-char image name
    # (verified on Windows 10/11; the /FI filter form is the belt-and-braces).
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
                "Stop-Process -Name hev-socks5-tunnel,"
                " hev-socks5-tun -Force -ErrorAction SilentlyContinue",
            ],
            timeout=15,
        )
    except Exception:
        pass
    # few seconds to actually reap a force-killed process; a reconnect issued
    # in that window saw the old tunnel still holding the TUN adapter ("always
    # occupied, cannot connect"). Poll until the image names are gone (max ~6s).
    leftover = ()
    for _ in range(12):
        leftover = _find_foreign_engine_procs()
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
        leftover = _find_foreign_engine_procs()
    if log_fn:
        if leftover:
            log_fn(f"Warning: some leftover engine processes could not be terminated ({', '.join(leftover)}).")
        else:
            log_fn("All leftover engine processes have been ended.")
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

# binaries bundled with this package (verified against upstream at build time,
# see DEPENDENCY_LOCK.md). Because this program runs with administrator rights,
# an swapped cached binary would be a real privilege-escalation vector, so both
# the installed copies and the bundled copies are re-verified before every use.
EXPECTED_BIN_SHA256 = {
    # 以下为随安装包 engine/ 目录提供的原生二进制，构建时 SHA-256 锁定，
    # 运行时再次校验（程序以管理员权限运行，防止被替换的缓存二进制提权）。
    "hev-socks5-tunnel.exe": "242557dc4ada5a45bcc02d98a558e86dd29025efdf7960b8e302bfc2ad1901c5",
    "wintun.dll": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
    "msys-2.0.dll": "1410599ee2efcede0869abec8910398d688755952e59cf7983b15cac6de78201",
}

# （engine.ADAPTER_GUID → YAML tunnel.guid，WintunCreateAdapter 按 GUID 重开），
# 适配器名为 LinkHelm-IwIP；启动时 _cleanup_stale_tun_adapters 只清除旧模式
# （LinkHelm/LinkHelm N/LaptopProxyVPN*，含 tun2socks 遗留）而保留本适配器，
# 实现"先清旧 → 建新 → 后续复用"，不再出现 "LinkHelm-IwIP 2/3/4"。

def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fin:
        for chunk in iter(lambda: fin.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _verify_bins(pairs):
    """Verify a list of (name, path) against the integrity lock.

    `name` indexes EXPECTED_BIN_SHA256; entries not in the lock are skipped
    (best-effort). Returns (ok, bad_name_or_None); missing files fail.
    """
    for name, path in pairs:
        expected = EXPECTED_BIN_SHA256.get(name)
        if expected is None:
            continue
        if not os.path.exists(path) or _file_sha256(path) != expected:
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

def tunnel_udp_dns_check(dns_ip="223.5.5.5", dns_port=53, timeout=5.0):
    """beta1.0.51: through-tunnel UDP probe — the REAL data plane games use.

    socks5_udp_dns_check talks to the phone's SOCKS5 relay DIRECTLY (one
    control connection + one datagram socket, single source port): it proves
    the relay answers, NOT that the tunnel data plane works. That is exactly
    how "UDP OK" shipped while games failed — and why the Windows DNS client
    (queries to the tunnel's resolvers travel TUN -> lwIP engine -> phone
    relay) died with ERR_NAME_NOT_RESOLVED.

    This probe walks the real path: it binds a plain UDP socket to the TUN
    adapter address and sends a DNS query to the internet. With the tunnel up,
    the /1 catch-all routes force the datagram into the adapter
    (hev -> phone relay -> resolver -> back). Binding to the TUN source IP is
    what makes the result honest: if the tunnel is down, the address does not
    exist and the bind fails — the probe can never pass by leaking out the
    physical NIC. Returns (True, description) / (False, reason).
    """
    sock = None
    bind_error = None
    for bind_ip in ("10.0.0.2", "10.0.0.1"):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((bind_ip, 0))  # force the TUN source address
            break
        except OSError as exc:
            bind_error = exc
            try:
                sock.close()
            except Exception:
                pass
            sock = None
    if sock is None:
        return False, f"tunnel address unavailable ({type(bind_error).__name__}: {bind_error})"
    try:
        sock.settimeout(timeout)
        qid = random.randint(1, 0xFFFF)
        sock.sendto(_build_dns_query(qid), (dns_ip, dns_port))
        data, _addr = sock.recvfrom(2048)
        if len(data) < 12:
            return False, "UDP reply through the tunnel too short"
        resp_id = (data[0] << 8) | data[1]
        if resp_id != qid or not (data[2] & 0x80):
            return False, "UDP reply received through the tunnel but not a valid DNS response"
        return True, (
            f"UDP tunnel data plane reachable (DNS query {dns_ip}:{dns_port} answered via "
            "TUN -> lwIP engine -> phone relay)"
        )
    except socket.timeout:
        return False, f"UDP timed out through the tunnel (no response within {timeout:.0f}s)"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

def tunnel_tcp_check(host="www.baidu.com", port=80, timeout=8.0):
    """beta1.0.51: through-tunnel TCP probe — end-to-end validation of the TCP
    data plane (TUN -> lwIP engine -> phone relay -> internet), source-bound
    to the TUN address so it can never leak out the physical NIC. Sends a
    minimal HTTP HEAD and accepts any HTTP status line as proof the path
    works. Returns (True, description) / (False, reason); never raises.
    """
    sock = None
    bind_error = None
    for bind_ip in ("10.0.0.2", "10.0.0.1"):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((bind_ip, 0))
            break
        except OSError as exc:
            bind_error = exc
            try:
                sock.close()
            except Exception:
                pass
            sock = None
    if sock is None:
        return False, f"tunnel address unavailable ({type(bind_error).__name__}: {bind_error})"
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.sendall(f"HEAD / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
        data = sock.recv(64)
        if data.startswith(b"HTTP/"):
            return True, f"TCP tunnel data plane reachable ({host}:{port} answered through the tunnel)"
        return False, "TCP connected through the tunnel but no HTTP response"
    except socket.timeout:
        return False, f"TCP timed out through the tunnel (no response within {timeout:.0f}s)"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            sock.close()
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
    Manages the native lwIP engine (hev-socks5-tunnel).
    Creates a Virtual Network Adapter to capture 100% of Windows traffic.
    """
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
        self.socks_user = socks_user or DEFAULT_PHONE_USER
        self.socks_pass = socks_pass or DEFAULT_PHONE_PASS
        self.log = log_fn
        self.process = None
        self.supervisor = None
        self._monitoring_active = False
        self._connection_healthy = False
        self._restarting = False
        # Patch B: optional callback consulted before any dependency download.
        self.confirm_download = confirm_download
        self._owned_routes = []
        # Round-2 fix (P1-W2): gateway of the host route back to the phone, so cleanup
        # can remove exactly what we added.
        self._phone_route_gw = None
        # the lwIP engine dials the primary lane (8080) directly — the proven 1.4.93
        # data plane. Only when the primary times out on several consecutive
        # short probes does the tunnel move to the standby 8082 lane, and it
        # moves back the moment the primary answers again.
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        # Windows never renames a reopened adapter, so a drifted name
        # ("LinkHelm 31") makes every netsh name= call a silent no-op — no
        # IP/DNS on the tunnel → ERR_NAME_NOT_RESOLVED + "DNS配置异常" scans.
        # _normalize_adapter_connection_name keeps this canonical.
        self._tun_conn_name = None
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
        # as a side effect of the old `netsh set address ... <gateway>` config)
        # is ours too — a 0.0.0.0/0 pointing at 10.0.0.1 black-holes the WHOLE
        # PC the moment the engine stops (the reported "检测时电脑断网"). The /1
        # pair already covers the entire unicast space; the default route is
        # redundant while sharing and lethal afterwards.
        if OUR_TUN_GATEWAY in self._find_route_gateways("0.0.0.0", "0.0.0.0"):
            self.log(f"Removing automatic default route 0.0.0.0/0 via {OUR_TUN_GATEWAY} (added by LaptopProxy)...")
            subprocess.run(
                ["route", "delete", "0.0.0.0", "mask", "0.0.0.0", OUR_TUN_GATEWAY],
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
        #
        # beta1.0.53/1.0.54 zeroed it in this method, but _clean_routes() runs
        # on every rollback path — including the one right before
        # _restore_ncsi_probing(). Since _restore_ncsi_probing() returns early
        # when _ncsi_restore is None, the pending value was destroyed before it
        # could be written back and Windows' connectivity probe stayed disabled
        # for the rest of the session. Restoring is owned solely by
        # _restore_ncsi_probing() (plus the atexit net).

    def _find_route_gateways(self, dest, mask):
        """Return the list of gateways currently serving dest/mask (empty = none)."""
        return _route_gateways(dest, mask)

    def _wait_adapter_ip_ready(self, timeout_s):
        """beta1.0.5: poll until the LinkHelm-IwIP adapter reports an address.

        hev-socks5-tunnel assigns tunnel.ipv4 (10.0.0.1/32) to the adapter at
        every start; _configure_adapter_if_needed then layers the routable
        10.0.0.2/24 (gateway 10.0.0.1) on top via netsh. Either address means
        the adapter is up — waiting for BOTH would always burn the full timeout
        on the first launch. With the fixed-GUID adapter reused across runs
        (engine.ADAPTER_GUID) this is typically instant. Returns False on
        timeout or if the lwIP engine exited early.

        beta1.0.51: addresses the adapter by its RESOLVED connection name
        (self._tun_conn_name) — the hardcoded canonical name silently matched
        nothing on machines where the name had drifted.
        """
        name = self._tun_conn_name or TUN_ADAPTER_NAME
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                return False
            _, out, _ = _run_decoded(
                ["netsh", "interface", "ipv4", "show", "addresses", f"name={name}"],
                timeout=15,
            )
            if "10.0.0.1" in (out or "") or "10.0.0.2" in (out or ""):
                return True
            time.sleep(0.25)
        return False

    def _normalize_adapter_connection_name(self):
        """beta1.0.51: make the tunnel's Windows connection name canonical.

        The network flyout shows the CONNECTION name, and every netsh config
        call addresses the adapter by it. When it drifted (the field
        "LinkHelm 31" entry), the IP/DNS configuration silently applied to
        nothing: Windows had no resolver on the tunnel, pages died with
        ERR_NAME_NOT_RESOLVED, and computer-manager DNS scans flagged 异常.
        Renaming back to the canonical name fixes the display AND the
        configuration in one step. Idempotent — a no-op when already
        canonical. Returns the name now in force (for subsequent netsh calls).
        """
        actual = _resolve_tun_connection_name()
        if not actual:
            # Adapter not resolvable yet — keep the canonical name; the
            # caller's netsh calls will surface any real problem via the
            # follow-up probe, exactly as before beta1.0.51.
            return TUN_ADAPTER_NAME
        if actual == TUN_ADAPTER_NAME:
            return actual
        self.log(
            f"Tunnel connection name drifted to \"{actual}\"; renaming to "
            f"\"{TUN_ADAPTER_NAME}\" (fixes the network-list entry and makes "
            "adapter IP/DNS configuration effective again)."
        )
        try:
            res = subprocess.run(
                ["netsh", "interface", "set", "interface", "name", actual,
                 "newname", TUN_ADAPTER_NAME],
                capture_output=True, timeout=20,
            )
        except Exception as error:
            self.log(f"Connection-name normalization failed ({error}); continuing with \"{actual}\".")
            return actual
        if res.returncode == 0:
            self.log(f"Connection name normalized to \"{TUN_ADAPTER_NAME}\".")
            return TUN_ADAPTER_NAME
        self.log(
            f"Connection-name normalization failed (rc={res.returncode}); "
            f"continuing with \"{actual}\" — adapter config still works by that name."
        )
        return actual

    def _configure_adapter_if_needed(self):
        """beta1.0.55: apply the adapter config and self-heal a drifted name.

        Windows never renames an adapter on its own, but a Wintun adapter that
        was created while an older one still existed comes up as
        "LinkHelm 2/3/.../31". Every `netsh ... name=LinkHelm-IwIP` call then
        matches nothing and silently does nothing — the tunnel ends up with no
        address and no resolver, which is exactly the beta1.0.54 field report
        ("TCP 和 UDP 不通", WinError 10049/10051 on the tunnel probes while the
        phone relay still answers).

        So the apply step is now verified, and a failed apply triggers one
        re-normalise-and-retry cycle instead of silently continuing.
        """
        for attempt in (1, 2):
            if self._apply_adapter_config_once():
                return
            if attempt == 1:
                self.log(
                    "Tunnel adapter configuration did not take effect — the "
                    "connection name has most likely drifted (the network list "
                    "shows something like \"LinkHelm 31\"). Re-normalising it "
                    "and applying the configuration once more."
                )
                self._tun_conn_name = self._normalize_adapter_connection_name()

    def _apply_adapter_config_once(self):
        """beta1.0.5: idempotent adapter config — reuse what is correct.

        Returns True when the adapter ends up with a usable resolver.

        lwIP (hev-socks5-tunnel) recreates a fresh LinkHelm adapter each run,
        so re-running `netsh set` on every start is wasted work AND extra churn
        on the network stack (each set can bounce NLA state). Check first:
        - address already 10.0.0.2/24 via 10.0.0.1 → skip `set address`
        - DNS already 223.5.5.5 (+119.29.29.29)   → skip `set/add dns`
        Only genuinely missing config is applied. Never raises: a failed
        config application must surface via the follow-up probe, not here.

        beta1.0.51 (DNS 连通性修复): the adapter is addressed by its RESOLVED
        connection name (self._tun_conn_name) — the hardcoded canonical name
        silently no-op'd on drifted machines, leaving the tunnel WITHOUT a
        resolver (ERR_NAME_NOT_RESOLVED, "DNS配置异常" scans). After applying,
        the DNS servers are VERIFIED on the adapter and a third domestic
        resolver (114.114.114.114) is added so one blocked/slow resolver
        cannot take name resolution down with it.
        """
        name = self._tun_conn_name or TUN_ADAPTER_NAME
        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "addresses", f"name={name}"],
            timeout=15,
        )
        if "10.0.0.2" in (out or ""):
            self.log("NIC IP is already 10.0.0.2/24; reusing existing config (skipped netsh set address).")
        else:
            self.log("Configuring IP...")
            try:
                # gateway made Windows install an automatic DEFAULT route
                # (0.0.0.0/0 via 10.0.0.1, metric 1) on this adapter — it beats
                # the WLAN default and black-holes the WHOLE PC whenever the
                # engine stops. Traffic steering is owned exclusively by the
                # tracked /1 catch-all routes (_add_owned_routes), which are
                # rolled back on stop; the on-link 10.0.0.0/24 route keeps the
                # adapter's own gateway address 10.0.0.1 reachable.
                subprocess.run(
                    ["netsh", "interface", "ip", "set", "address", f"name={name}",
                     "static", "10.0.0.2", "255.255.255.0"],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as error:
                # (the follow-up probe decides), but say exactly what happened.
                self.log(
                    f"WARNING: could not set the tunnel IP on \"{name}\" "
                    f"(rc={error.returncode}); the connection name may have drifted."
                )

        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "dnsservers", f"name={name}"],
            timeout=15,
        )
        dns_out = (out or "")
        if "223.5.5.5" in dns_out and "119.29.29.29" in dns_out:
            self.log("NIC DNS is already AliDNS/DNSPod; reusing existing config (skipped netsh set dns).")
        else:
            self.log("Configuring DNS...")
            # lookup abroad (the single biggest recurring cross-border traffic source, a
            # needless detection footprint on managed networks). AliDNS +
            # DNSPod are fast and domestic; resolution still travels the
            # same tunnel path.
            try:
                subprocess.run(
                    ["netsh", "interface", "ip", "set", "dns", f"name={name}",
                     "static", "223.5.5.5"],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["netsh", "interface", "ip", "add", "dns", f"name={name}",
                     "119.29.29.29", "index=2"],
                    check=False, capture_output=True,
                )
                # on timeouts, so one slow/blocked resolver no longer fails
                # every lookup (the "DNS配置异常" field symptom).
                subprocess.run(
                    ["netsh", "interface", "ip", "add", "dns", f"name={name}",
                     "114.114.114.114", "index=3"],
                    check=False, capture_output=True,
                )
            except subprocess.CalledProcessError as error:
                self.log(
                    f"WARNING: could not set the tunnel DNS on \"{name}\" "
                    f"(rc={error.returncode}); pages may fail with ERR_NAME_NOT_RESOLVED."
                )

        # A drifted connection name turns every netsh call into a silent
        # no-op — without this check the tunnel comes up resolver-less and
        # the first symptom the user sees is "无法访问此页面".
        _, out, _ = _run_decoded(
            ["netsh", "interface", "ipv4", "show", "dnsservers", f"name={name}"],
            timeout=15,
        )
        if any(s in (out or "") for s in ("223.5.5.5", "119.29.29.29", "114.114.114.114")):
            return True
        self.log(
            f"WARNING: no DNS servers are active on the tunnel adapter \"{name}\" — "
            "name resolution through the tunnel WILL fail (ERR_NAME_NOT_RESOLVED). "
            "If the Windows network list shows a drifted name like \"LinkHelm 31\", "
            "renaming the connection to \"LinkHelm-IwIP\" and restarting sharing fixes it."
        )
        return False

    def _restore_ncsi_probing(self):
        """beta1.0.55: put the Windows connectivity probe back the way we found it."""
        # session may have pointed them at the domestic endpoint).
        try:
            _restore_ncsi_probe(self.log)
        except Exception:
            pass
        previous = getattr(self, "_ncsi_restore", None)
        global _ACTIVE_TUN_MANAGER
        if _ACTIVE_TUN_MANAGER is self:
            _ACTIVE_TUN_MANAGER = None
        if previous is None:
            return
        self._ncsi_restore = None
        if _set_ncsi_probing(bool(previous), self.log):
            self.log("Restored the Windows connectivity-probe setting.")

    def _add_owned_routes(self):
        """Patch B / Round-2: add the return route to the phone plus the two /1
        catch-all routes, tracking exactly what we create.

        Routes that already exist (e.g. created by other VPN software) are left
        untouched and reported; the rollback path (_clean_routes) only ever removes
        routes pointing at our own TUN gateway or the saved phone return-route gw.
        """
        self._owned_routes = []

        # BEFORE adding anything. Two sources: (a) the old `netsh set address
        # ... <gateway>` config installed 0.0.0.0/0 via 10.0.0.1 as a side
        # effect, (b) a crashed previous run never cleaned it up. Either way it
        # black-holes the whole PC once the engine stops (metric 1 beats the
        # WLAN default) — the reported "检测时电脑断网". The /1 pair below is
        # the complete, tracked, rollback-able replacement.
        if OUR_TUN_GATEWAY in self._find_route_gateways("0.0.0.0", "0.0.0.0"):
            self.log(f"Removing stale default route 0.0.0.0/0 via {OUR_TUN_GATEWAY} (leftover from a previous run)...")
            subprocess.run(
                ["route", "delete", "0.0.0.0", "mask", "0.0.0.0", OUR_TUN_GATEWAY],
                capture_output=True,
            )

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
            # Without `IF`, Windows resolves the gateway 10.0.0.1 against
            # whichever interface currently has a path to it — live evidence:
            # 0.0.0.0/1 came up bound to the WLAN adapter, and every
            # destination in the lower half of the IP space then depended on
            # that accidental binding (intermittent timeouts = "连通性很差").
            # beta1.0.84: the C++ netops module does add-with-IF + readback
            # verification in one call (VERIFIED). Python fallback keeps the
            # legacy add/rebind dance for old packages.
            tun_idx = _tun_interface_index(self._tun_conn_name or TUN_ADAPTER_NAME)
            used_cpp = False
            if tun_idx:
                try:
                    import cpp_bridge
                    if cpp_bridge.netops_available():
                        used_cpp = cpp_bridge.add_route_verified(
                            dest, "128.0.0.0", OUR_TUN_GATEWAY, tun_idx)
                        if used_cpp:
                            self._owned_routes.append(dest)
                            continue
                except Exception:
                    used_cpp = False
            cmd = ["route", "add", dest, "mask", "128.0.0.0",
                   OUR_TUN_GATEWAY, "metric", "1"]
            if tun_idx:
                cmd += ["IF", str(tun_idx)]
            rc, out, err = _run_decoded(cmd, timeout=15)
            if rc != 0 and tun_idx:
                # A wrong/unknown IF index makes route add fail outright —
                # fall back to the unbound form, then verify below.
                rc, out, err = _run_decoded(
                    ["route", "add", dest, "mask", "128.0.0.0",
                     OUR_TUN_GATEWAY, "metric", "1"], timeout=15)
            if rc != 0:
                self._clean_routes()
                detail = (err or out or "").strip()
                raise RuntimeError(f"route add {dest}/1 failed: {detail}")
            self._owned_routes.append(dest)
            # (route print reports the interface as its own IP).
            if tun_idx:
                bindings = [i for g, i in _route_bindings(dest, "128.0.0.0")
                            if g == OUR_TUN_GATEWAY]
                if bindings and TUN_LOCAL_IP not in bindings:
                    self.log(
                        f"Route {dest}/1 bound to interface(s) {bindings}, "
                        f"expected the TUN ({TUN_LOCAL_IP}) — re-binding."
                    )
                    subprocess.run(
                        ["route", "delete", dest, "mask", "128.0.0.0",
                         OUR_TUN_GATEWAY], capture_output=True)
                    _run_decoded(cmd, timeout=15)
                    bindings = [i for g, i in
                                _route_bindings(dest, "128.0.0.0")
                                if g == OUR_TUN_GATEWAY]
                    if TUN_LOCAL_IP in bindings:
                        self.log(f"Route {dest}/1 re-bound to the TUN interface.")
                    else:
                        self.log(
                            f"Warning: route {dest}/1 still not on the TUN "
                            f"interface (now {bindings}) — connectivity for "
                            "part of the address space may be unstable."
                        )

    def _check_dependencies(self):
        """beta1.0.5: verify the bundled lwIP engine (hev-socks5-tunnel) + Wintun.

        tun2socks has been removed entirely; the native lwIP engine ships with
        the package under engine/ (resolved via engine.engine_bin_dir() — the
        same location LwipEngine launches from: <pkg>/engine/ from source,
        <app>/_internal/engine/ in the frozen onedir package) and is the ONLY
        user-space protocol stack. No runtime download is attempted — if the
        bundle is incomplete the user must reinstall. Runs elevated, so we
        re-verify SHA-256 before trusting. Returns the lwIP engine exe path
        (used for logging); raises on failure.
        """
        from engine import (
            LWIP_EXE_NAME, MSYS_DLL_NAME, WINTUN_DLL_NAME, engine_trio_paths,
        )
        exe_path, wt_path, msys_path = engine_trio_paths()
        engine_dir = os.path.dirname(exe_path)

        # Re-verify the integrity-locked binaries (elevated process — guard
        # against a swapped cached copy acting as a privilege-escalation vector).
        ok, bad = _verify_bins([
            (LWIP_EXE_NAME, exe_path),
            (WINTUN_DLL_NAME, wt_path),
            (MSYS_DLL_NAME, msys_path),
        ])
        if not ok:
            msg = (
                f"lwIP engine component '{bad}' is missing or fails SHA-256 "
                f"verification in {engine_dir}. Please reinstall the LinkHelm "
                "package (engine/ must contain hev-socks5-tunnel.exe, "
                "wintun.dll and msys-2.0.dll)."
            )
            self.log(msg)
            raise RuntimeError(msg)
        self.log(
            f"lwIP engine verified: hev-socks5-tunnel (v{LWIP_ENGINE_VERSION}), "
            f"Wintun v{WINTUN_VERSION}."
        )
        return exe_path

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
        # crashed run, or started by an old pre-1.4.5 exe that has no mutex)
        # keeps the old VPN alive and its adapter name climbing. Kill it and
        # proceed — refusing (v1.4.62) read to the user as "reconnect does
        # nothing". Only if the kill itself fails do we stop.
        _kill_engine_procs_all(log_fn=self.log)
        if _find_foreign_engine_procs():
            raise RuntimeError(
                "Leftover connection processes could not be ended; a new connection cannot be started safely.\n"
                "Open Task Manager, end hev-socks5-tunnel.exe, then retry."
            )
        # Round-2 fix (P0-W1): verify the phone's SOCKS5 endpoint BEFORE touching
        # Wintun, IP configuration or routes. A phone-side failure now surfaces as a
        # precise error instead of a mysterious TUN/routing fault later.
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

        # runs — otherwise Windows keeps piling up "LinkHelm 14/27/..."
        # dead entries in the network list. Best effort; the fresh adapter for
        # this run is created right after.
        # `pnputil /remove-device` physically removes a kernel PnP device; if a
        # prior crashed/failed run (1.0.53/1.0.54) left an orphaned Wintun
        # adapter still held by the kernel or a zombie engine, forcing the
        # removal at CONNECT time bluescreens the machine. Zombie engines are
        # already reaped by the _kill_engine_procs_all() call above, and the live
        # adapter is reused by its fixed GUID, so the only thing this sweep ever
        # bought was cosmetic dead entries in the network list — not worth a
        # BSOD. The helper is kept as a manual hygiene tool only.
        #
        # beta1.0.52 added `_cleanup_stale_wlan_profiles()` to tidy the numbered
        # duplicates ("LinkHelm 2" .. "LinkHelm 31") in the network list. It
        # deletes saved profiles with `netsh wlan delete profile`, and on
        # Windows deleting the profile of the network you are CURRENTLY
        # associated with tears that association down — the PC drops off
        # Wi-Fi. Because the Wi-Fi Direct group re-forms with a fresh BSSID
        # every session, the live connection is very often one of the very
        # numbered profiles being swept, so the symptom was:
        #   "检测连通性 / 连接失败时电脑会断开 WiFi".
        # The cosmetic gain (fewer dead entries) is not worth a dropped link,
        # and the user-visible display problem is now handled by normalising
        # the tunnel's own connection name instead (see
        # _normalize_adapter_connection_name), which cannot touch Wi-Fi state.
        # The helper is kept below but is deliberately no longer auto-invoked.

        # beta1.0.72 "respect a 0" guard was wrong in practice: the 0 it
        # respected was itself the stale leftover of the beta1.0.55 pause —
        # it never healed, and the flyout kept saying "无法访问 Internet"
        # forever (re-confirmed live on 1.0.72). The pre-session value is
        # still remembered in _ncsi_restore and restored on every exit path;
        # during a session the probe now points at a domestic endpoint and
        # actually validates the link.
        present, previous = _read_ncsi_probing()
        # 1 is the Windows default when the value has never been written.
        self._ncsi_restore = previous if present else 1
        global _ACTIVE_TUN_MANAGER
        _ACTIVE_TUN_MANAGER = self
        if _set_ncsi_probing(True, self.log):
            self.log(
                "Windows connectivity probe stays ON at Microsoft defaults: "
                "with the tunnel up the probe endpoints are reachable through "
                "the phone's upstream, which is what keeps the network list "
                "showing \"Internet\" (verified live 1.0.82)."
            )
        _set_domestic_ncsi_probe(self.log)

        # 启动时先校验内置原生引擎完整性，再通过 EngineManager 拉起 lwIP。
        self._check_dependencies()
        from engine import ENGINE_MANAGER
        (exe, args, cwd), engine_name, engine_note = ENGINE_MANAGER.build_launch(
            self._socks_proxy_url()
        )
        self.log(f"Engine selected: {engine_name}")
        if engine_note:
            self.log(f"[engine] {engine_note}")

        # Hide window on Windows, capture output
        # Job Object (kernel-guaranteed cleanup: supervisor dies -> engines
        # die; python dies -> pipe EOF -> engines die). Python keeps a shim
        # "process" object only for poll()/pid compat with the monitor code.
        self.supervisor = None
        pid = 0
        try:
            from engine_supervisor import EngineSupervisor
            sup = EngineSupervisor(log_fn=self.log)
            if sup.ensure_running():
                pid = sup.start_engine(exe, args)
                if pid:
                    self.supervisor = sup
                    self.log(f"[supervisor] engine pid {pid} inside C++ job object")
        except Exception as sup_err:
            self.log(f"[supervisor] unavailable ({sup_err}); using legacy spawn")
        if self.supervisor is not None:
            class _SupervisorProcShim:
                """Popen-compatible view over the supervisor-owned engine."""
                def __init__(self, pid, sup):
                    self.pid = pid
                    self._sup = sup
                    self.stdout = None
                    self.stderr = None
                def poll(self):
                    return None if self._sup.engine_running() else 0
                def terminate(self):
                    self._sup.stop_engine()
                def kill(self):
                    self._sup.stop_engine()
                def wait(self, timeout=None):
                    return 0
            self.process = _SupervisorProcShim(pid, self.supervisor)
        else:
            cflags = 0x08000000 if IS_WINDOWS else 0
            self.process = subprocess.Popen([exe, *args], cwd=cwd, creationflags=cflags,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._read_engine_output()

        self.log("Waiting for the adapter to be ready...")
        adapter_found = False
        for _ in range(15):
            if self.process.poll() is not None:
                raise RuntimeError("lwIP engine exited before its adapter was ready.")
            # so the adapter was never detected and startup rolled back even
            # though the lwIP engine had created it successfully.
            # name — a drifted connection name ("LinkHelm 31") must still be
            # recognized here (and normalized right after), never treated as
            # "adapter missing" (which aborted startup outright).
            _, out, _ = _run_decoded(["netsh", "interface", "ipv4", "show", "interfaces"], timeout=15)
            if "linkhelm" in (out or "").lower():
                adapter_found = True
                break
            time.sleep(1)

        if not adapter_found:
            self.stop()
            raise Exception("Failed to create Virtual Adapter.")

        # A drifted name made every name= call a silent no-op: the tunnel came
        # up without IP/DNS, pages failed with ERR_NAME_NOT_RESOLVED, and the
        # Windows network list showed a confusing duplicate ("LinkHelm 31").
        self._tun_conn_name = self._normalize_adapter_connection_name()

        # instead of the old blind 2.0s+1.0s sleeps (faster reconnect).
        if not self._wait_adapter_ip_ready(5.0):
            self.log("Adapter IP not ready immediately; falling back to a fixed wait.")
            time.sleep(1.0)
        self._configure_adapter_if_needed()
        # the tunnel was flapping stay cached (ERR_NAME_NOT_RESOLVED) and keep
        # breaking page loads long after the data plane is healthy again.
        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=15)
            self.log("Cleared the Windows DNS cache (stale lookup failures from the reconnect window).")
        except Exception:
            pass
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

        # field reports ("UDP OK but games fail", ERR_NAME_NOT_RESOLVED,
        # "TCP 连通性很差") all trace back to the tunnel data plane, so prove
        # it (or disprove it) in the log right here instead of guessing later.
        # Diagnostic only: a failure never tears the tunnel down.
        self._log_data_plane_selftest()

        # Start health monitors
        self._monitoring_active = True
        self._connection_healthy = True
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        self._lane_b_since = 0.0
        self._lane_a_recover_strikes = 0
        threading.Thread(target=self._monitor_process, daemon=True).start()
        threading.Thread(target=self._monitor_health, daemon=True).start()
        threading.Thread(target=self._monitor_lane_failover, daemon=True).start()
        # Regular light traffic keeps the Wi-Fi Direct link out of power-save,
        # which is the root cause of the "dial 192.168.49.1:8080 i/o timeout"
        # storms; and sustained warmer failures trigger an EARLY rebuild check
        # so a genuine drop reconnects in seconds instead of tens of seconds.
        threading.Thread(target=self._link_keepalive_loop, daemon=True).start()
        # sleep / Modern Standby suspends the lwIP engine and the Wi-Fi Direct link
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
          _restart_engine sequence (the crash watchdog may race us; its
          _restarting mutex makes the second call a no-op).
        - still unreachable after the full window → bounded rebuild; if the
          phone itself is still away, the precheck fails and _restart_engine
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
                # latency is 3-8s, so a 4s probe could fail a link that was
                # merely still waking and trigger a needless rebuild.
                if self._probe_ok(timeout=8.0):
                    recovered = True
                    break
            if not self._monitoring_active or self._restarting:
                continue
            if recovered:
                self.log("Link verified after wake; no rebuild needed.")
            else:
                self.log("Link not recovered after wake; entering bounded rebuild.")
                self._restart_engine()

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
        # notification — a queued wake callback must never restart the engine
        # after the user stopped sharing. getattr-guarded: tests may build
        # instances via object.__new__ without __init__ state.
        resume_event = getattr(self, "_resume_event", None)
        if resume_event is not None:
            resume_event.set()  # wake the monitor thread so it exits promptly
        _unregister_resume_notification(getattr(self, "_resume_reg", None))
        self._resume_reg = None
        self._crash_times = []
        self._use_lane_b = False
        self._lane_a_fail_strikes = 0
        self._lane_b_since = 0.0
        self._lane_a_recover_strikes = 0
        self._restore_ncsi_probing()
        self.log("Restoring routes...")
        self._clean_routes()

        if self.process:
            self.log("Stopping connection processes...")
            # side closes the job (kernel kills the tree). No taskkill sweep.
            if self.supervisor is not None:
                try:
                    self.supervisor.stop_engine()
                    self.log("[supervisor] engine terminated via job close.")
                except Exception as sup_err:
                    self.log(f"[supervisor] stop failed ({sup_err}); falling back.")
                self.process = None
            else:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                except Exception:
                    pass
                # This is what guarantees "exit means exit": a graceful terminate
                # that silently fails must never leave the engine child (and
                # with it the VPN tunnel) running in the background.
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
                self.process = None
            
        subprocess.run(["netsh", "interface", "set", "interface", "LinkHelm-IwIP", "disable"], capture_output=True)
        # v1.4.91/beta1.0.83: with the supervisor path the engine died via the
        # job close above; the name-based sweep now only reaps FOREIGN orphans
        # left by crashed old builds (cheap when nothing matches).
        _kill_engine_procs_all(log_fn=self.log)
        # relaunch on the next start; holding it adds no value while idle).
        if self.supervisor is not None:
            try:
                self.supervisor.shutdown()
            except Exception:
                pass
            self.supervisor = None
        # next session starts clean. Legacy engine teardown is a no-op.
        try:
            from engine import ENGINE_MANAGER
            ENGINE_MANAGER.teardown()
        except Exception:
            pass
        # Windows files yet another drifted "LinkHelm N" profile. Sweep the
        # ghosts now so the flyout never grows a new number per session.
        try:
            sweep_ghost_linkhelm_networks(self.log)
        except Exception:
            pass
        self.log("Connection stopped.")

    def _read_engine_output(self):
        """Read and log the lwIP engine (hev-socks5-tunnel) stdout/stderr in background threads."""
        def _annotate(msg):
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
                        self.log(f"[lwip:{label}] {_annotate(msg)}")
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
        # the existing _restart_engine bounded sequence (its _restarting
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
                    f"Error: lwIP engine (PID {pid}, exit code {exit_code}) crashed "
                    f"within {CRASH_COOLDOWN_WINDOW_S // 60} minute(s) "
                    f"{CRASH_COOLDOWN_MAX} times; stopping auto-restart (FAILED_COOLDOWN),"
                    "check the log, then reconnect manually."
                )
                return
            self.log(
                f"Watchdog: lwIP engine process exited (PID {pid}, exit code {exit_code}),"
                "auto-restarting..."
            )
            self._restart_engine()
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

    def _socks_proxy_url(self):
        """The SOCKS5 URL the lwIP engine should dial for the current mode.

        v1.5.47: credentials embedded (RFC 1929 userpass). Alphanumeric-only
        credentials need no URL escaping; quote() is applied anyway for safety.
        """
        user = urllib.parse.quote(self.socks_user, safe="")
        pwd = urllib.parse.quote(self.socks_pass, safe="")
        if self._use_lane_b:
            return f"socks5://{user}:{pwd}@{self.phone_ip}:{SECONDARY_LANE_PORT}"
        return f"socks5://{user}:{pwd}@{self.phone_ip}:{self.phone_port}"

    def _log_data_plane_selftest(self):
        """beta1.0.51: prove (or disprove) the tunnel data plane in the log.

        Runs the through-tunnel UDP (DNS) and TCP probes once at startup.
        These walk the exact path user traffic takes (TUN -> lwIP engine ->
        phone relay -> internet), source-bound to the TUN address so they can
        never leak out the physical NIC — unlike the direct relay probe, which
        used to report "UDP OK" while games and the Windows DNS client were
        failing. Diagnostic only: nothing here changes tunnel state.
        """
        try:
            ok, detail = tunnel_udp_dns_check()
            self.log(("✓ " if ok else "✗ ") + f"UDP data plane: {detail}")
            if not ok:
                self.log(
                    "  UDP through the tunnel is failing — games/voice will not work and DNS"
                    " lookups can time out (ERR_NAME_NOT_RESOLVED). The phone relay itself may"
                    " still answer a DIRECT probe; the break is inside the tunnel, not the phone."
                )
        except Exception as error:
            self.log(f"UDP data-plane self-test error: {error}")
        try:
            ok, detail = tunnel_tcp_check()
            self.log(("✓ " if ok else "✗ ") + f"TCP data plane: {detail}")
        except Exception as error:
            self.log(f"TCP data-plane self-test error: {error}")

    def _probe_ok(self, timeout=8.0):
        """v1.4.2: single lightweight reachability probe (no state changes).
        v1.4.92: default timeout 4s → 6s — Wi-Fi Direct power-save wake takes
        3-8s, and a too-short timeout manufactured false strikes.
        beta1.0.51: 6s → 8s — the documented wake latency tops out at 8s, so
        even the old default could still count a normal wake as an outage
        ("检测杀死好连接"); the probe now covers the full wake window.
        """
        # greeting level (the server is genuinely alive — the original
        # the "hollow handshake" was bare TCP). While failed over to :8082, lane A being
        # alive must not mask a standby-lane outage (and vice versa).
        active_port = SECONDARY_LANE_PORT if self._use_lane_b else self.phone_port
        return self._probe_socks_port(active_port, timeout=timeout)

    def _monitor_health(self):
        # A Wi-Fi Direct power-save blip lasts a few seconds; the old logic
        # (15s interval, single probe, restart after 2 strikes ≈ 30s) tore down
        # and rebuilt the whole tunnel far too eagerly — that was the reported
        # "occasional disconnects and stutter". Now: probe -> confirm 3s later -> count strike;
        # sharing stops at 2 strikes, tunnel rebuild only at 3.
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
                # The field report was exactly this "kill": two probes time out
                # during a Wi-Fi Direct power-save blip, sharing is torn down,
                # and the link self-heals moments later ("检测杀死好连接").
                # Sharing now stops ONLY on a CONFIRMED drop — the strike-3
                # long-probe gate below; a flapping link keeps its session.
                self._connection_healthy = False
                self.log("Proxy health check failed (2/3); sharing KEPT while the link may self-heal (no teardown on unconfirmed timeouts)")
            elif fail_count >= 3:
                self._connection_healthy = False
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
                self._restart_engine()
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
                # and before any switch an 8s confirm probe must ALSO fail
                # (beta1.0.51: 6s → 8s to cover the full Wi-Fi Direct wake
                # window — a 6s confirm still false-fired on slow wakes).
                if self._lane_a_fail_strikes < 5:
                    self.log(
                        f"Primary :8080 probe timed out ({self._lane_a_fail_strikes}/5); observing..."
                    )
                    continue
                if self._probe_socks_port(self.phone_port, timeout=8.0):
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
                # primary failure is the proxy or the phone's own uplink.
                self.log(
                    f"Primary :8080 egress relay diagnostics (logged only):"
                    f"{self._socks_relay_diag(self.phone_port)}"
                )
                self.log("Primary channel :8080 unreachable; switching to backup channel :8082 ...")
                self._use_lane_b = True
                self._lane_b_since = time.time()
                self._lane_a_recover_strikes = 0
                self._restart_engine()
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
                    self._restart_engine()
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
                        self._restart_engine()
                else:
                    self._lane_a_recover_strikes = 0
                    if not b_ok:
                        self.log("Backup channel :8082 probe timed out; awaiting health-check decision.")

    def _restart_engine(self):
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
        if self._use_lane_b:
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

        # can accumulate engine processes that are NOT our current child;
        # a new instance started while one of those holds the TUN adapter is
        # exactly the "reconnect fails (occupied)" symptom.
        _kill_engine_procs_all(log_fn=self.log)

        self._clean_routes()
        if not self._monitoring_active:
            return
        # risk as the initial start path (pnputil removes a kernel device that
        # may still be referenced). Zombies are already killed by the call above
        # and the adapter is reused by GUID, so never physically remove devices
        # during a live rebuild.
        # EngineManager (single Wintun owner).
        self._check_dependencies()
        from engine import ENGINE_MANAGER
        (exe, args, cwd), engine_name, engine_note = ENGINE_MANAGER.build_launch(
            self._socks_proxy_url()
        )
        self.log(f"Engine re-selected: {engine_name}")
        if engine_note:
            self.log(f"[engine] {engine_note}")
        cflags = 0x08000000 if IS_WINDOWS else 0
        self.process = subprocess.Popen(
            [exe, *args], cwd=cwd, creationflags=cflags,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._read_engine_output()
        self.log("Waiting for the adapter to be ready...")
        for _ in range(15):
            if self.process.poll() is not None:
                raise RuntimeError("lwIP engine exited while restarting.")
            # must not read as "adapter missing"), then normalize it below.
            _, out, _ = _run_decoded(
                ["netsh", "interface", "ipv4", "show", "interfaces"], timeout=15
            )
            if "linkhelm" in (out or "").lower():
                break
            time.sleep(1)
        else:
            raise RuntimeError("VPN adapter did not reappear.")

        # start path) — otherwise a drifted name silently skips IP/DNS setup.
        self._tun_conn_name = self._normalize_adapter_connection_name()

        # v1.5.41 (P0): idempotent config — reuse what is already correct.
        self._configure_adapter_if_needed()
        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=15)
        except Exception:
            pass
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
        self.root.title("LinkHelm Desktop beta1.0.85 | Windows")
        self.root.geometry(f"460x{min(770, max(620, root.winfo_screenheight() - 90))}")
        self.root.minsize(420, 590)
        self.root.configure(fg_color=self.BG)

        self.tun_mgr = None
        # any pre-beta1.0.52 build) can leave the automatic default route
        # 0.0.0.0/0 via 10.0.0.1 behind, which black-holes the whole PC the
        # moment the engine is not running ("检测时电脑断网"). The app is
        # elevated, so the sweep can actually run; start() re-adds the tracked
        # routes when the user connects. Best-effort, never blocks the UI.
        threading.Thread(target=self._startup_route_sweep, daemon=True).start()
        self.ics_mgr = IcsManager(self.log)
        self.accel = store_accel.StoreAccel(CONFIG_DIR, self.log)

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

        self.udp_button = ctk.CTkButton(
            self.content, text=tr("Check UDP Connectivity", "检测 UDP 连通性"), height=30,
            fg_color="transparent", border_width=1, border_color=self.BORDER,
            text_color=self.TEXT, hover_color=self.STATUS_BG,
            font=("Segoe UI", 11), command=self._on_udp_check
        )
        self.udp_button.pack(fill="x", pady=(0, 12))

        # v1.5.45 (beta1.0.1): file-transfer entry — single file <= 30 GB
        # (beta1.0.58, was 1 GB), resumable across a dropped link, over the
        # Wi-Fi Direct link only (port 8765), independent of the
        # tunnel; files never touch the phone's internet exit.
        # loopback-only Mihomo micro-kernel (the same engine Clash Verge uses)
        # and never touches the system proxy, DNS, hosts, adapters or routes —
        # so it COEXISTS with the user's own Clash Verge. All its controls live
        # in a dedicated page opened below (like the file-transfer assistant),
        # which also kills the old "starting the boost freezes the app" bug
        # (the previous card launched the kernel on the UI thread).
        accel_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        accel_card.pack(fill="x", pady=(0, 12))
        accel_header = ctk.CTkFrame(accel_card, fg_color="transparent")
        accel_header.pack(fill="x", padx=16, pady=9)
        ctk.CTkLabel(
            accel_header, text=tr("Store Boost", "商店加速"),
            font=("Segoe UI", 11, "bold"), text_color=self.TEXT
        ).pack(side="left")
        ctk.CTkButton(
            accel_header, text=tr("Open", "打开"), width=95, height=24,
            fg_color="transparent", hover_color=self.STATUS_BG,
            text_color=self.ACCENT, command=self._open_store_accel_dialog
        ).pack(side="right")
        ctk.CTkLabel(
            accel_card,
            text=tr(
                "Built-in open-source Clash core (same engine as Clash Verge). "
                "Runs as a loopback-only micro-kernel — never touches your system "
                "proxy, so it coexists with Clash Verge. Open the page, paste your "
                "subscription (or a clash:// one-click link), pick a node; it exposes "
                "a local proxy on 127.0.0.1 you opt into yourself.",
                "内置开源 Clash 内核（与 Clash Verge 同款）。仅作为本地回环微内核运行——"
                "不碰系统代理，因此可与 Clash Verge 共存。点开页面后粘贴订阅（或 clash:// "
                "一键导入链接）、选节点，它只暴露一个 127.0.0.1 本地代理，由你自行按需使用。"),
            font=("Segoe UI", 10), text_color=self.MUTED,
            wraplength=560, justify="left"
        ).pack(padx=16, pady=(0, 12))

        ft_card = ctk.CTkFrame(
            self.content, fg_color=self.CARD, corner_radius=13,
            border_width=1, border_color=self.BORDER
        )
        ft_card.pack(fill="x", pady=(12, 0))
        ft_header = ctk.CTkFrame(ft_card, fg_color="transparent")
        ft_header.pack(fill="x", padx=16, pady=9)
        ctk.CTkLabel(
            ft_header, text=tr("File Transfer", "文件传输"),
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

    def _startup_route_sweep(self):
        """beta1.0.55: heal stale tunnel routes without racing a live session.

        The sweep deletes 0.0.0.0/0, 0.0.0.0/1 and 128.0.0.0/1 when they point
        at our TUN gateway. Those are exactly the routes start() installs, so if
        the user connected while this thread was still pending it would delete
        the tunnel it was meant to protect — one more way to end up with a
        session that reports "TCP/UDP 不通". Bail out when an engine is up.
        """
        # 31") otherwise pile up forever in the network flyout, every one of
        # them showing "无法访问 Internet".
        try:
            _cleanup_stale_wlan_profiles(self.log)
        except Exception:
            pass
        # net-connection-profiles of our own lwIP adapter. Rename the live
        # drift to the fixed "LinkHelm" and delete the disconnected ghosts
        # from the NetworkList store (registry; needs the app's admin rights).
        try:
            sweep_ghost_linkhelm_networks(self.log)
        except Exception:
            pass
        # session — Microsoft's own probe hosts are unreachable without the
        # tunnel, so the flyout kept saying "无法访问 Internet".
        # (left by crashed 1.0.55..1.0.71 builds); restored at exit.
        try:
            _heal_ncsi_at_startup(self.log)
        except Exception:
            pass
        try:
            mgr = self.tun_mgr
            proc = getattr(mgr, "process", None) if mgr is not None else None
            if proc is not None and proc.poll() is None:
                self.log("Startup route sweep skipped: a tunnel is already running.")
                return
            _sweep_stale_tunnel_routes(self.log)
        except Exception:
            pass  # best effort — never break startup on hygiene

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

        # beta1.0.51 (P0): the VERDICT now comes from the THROUGH-TUNNEL probe —
        # the exact path games and the Windows DNS client take (TUN -> lwIP
        # engine -> phone relay). The direct relay probe below is demoted to a
        # diagnostic: it used to be the SOLE judge, which is how "UDP OK"
        # shipped while games failed with network errors and pages died with
        # ERR_NAME_NOT_RESOLVED (the relay answered one single-port flow; the
        # tunnel data plane was never exercised).
        failures = []
        for dns in pool:
            self.log(f"UDP check (through the tunnel): DNS query to {dns}:53 via TUN → lwIP engine → phone relay...")
            ok, detail = tunnel_udp_dns_check(dns_ip=dns)
            if ok:
                self._udp_dns_primary = dns
                self.root.after(0, lambda d=dns: self._udp_check_done(True, f"UDP OK via tunnel ({d})"))
                return
            failures.append(f"tunnel:{dns} {detail}")
            self.log(f"Through-tunnel probe via {dns} failed ({detail}); trying the next candidate...")

        # Tunnel data plane failed — diagnose WHICH half is broken so the log
        # says "phone relay dead" vs "tunnel broken" instead of one vague no.
        relay_reachable = False
        for dns in pool:
            label = tr("last success", "上次成功") if (dns == primary and primary) else tr("candidate {i}", "候选{i}").format(i=pool.index(dns) + 1)
            self.log(f"UDP relay diagnostic ({label}): direct DNS query via the phone SOCKS5 UDP relay to {dns}:53...")
            ok, detail = socks5_udp_dns_check(
                ip, port, dns_ip=dns,
                socks_user=getattr(self, "_socks_user", None) or DEFAULT_PHONE_USER,
                socks_pass=getattr(self, "_socks_pass", None) or DEFAULT_PHONE_PASS,
            )
            if ok:
                relay_reachable = True
                failures.append(f"relay:{dns} answers")
                break
            failures.append(f"relay:{dns} {detail}")
            self.log(f"Relay probe via {dns} failed ({detail}); trying the next candidate...")

        if relay_reachable:
            message = (
                "；".join(failures)
                + "；the phone relay itself answers, so the break is INSIDE the tunnel "
                  "(adapter/routes/engine), not the phone — UDP games will not work"
            )
        else:
            message = "；".join(failures)
        self.root.after(0, lambda m=message: self._udp_check_done(False, m))

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
                # it raises escapes log()'s own try/except and surfaces as
                # "Exception in Tkinter callback" — the classic teardown noise
                # when a worker thread logs one last line after the window is
                # already destroyed (log_text is gone → TclError). Degrade to
                # the console instead of raising inside a Tk callback.
                try:
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
                except Exception:
                    print(f"[{stamp}] {msg}")

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
    # follows the current Wi-Fi Direct lease), takes the peer's IP as input,
    # and requires one-tap mutual IP pairing before any transfer.
    def _open_file_transfer_dialog(self):
        phone_ip = self.phone_ip.get().strip() or "192.168.49.1"
        user = self.phone_user.get().strip() or DEFAULT_PHONE_USER
        password = self.phone_pass.get().strip() or DEFAULT_PHONE_PASS
        local_ip = file_transfer.local_ip_toward(phone_ip)

        win = ctk.CTkToplevel(self.root)
        win.title(tr("File Transfer", "文件传输"))
        win.geometry("470x470")
        win.transient(self.root)
        win.grab_set()

        ctk.CTkLabel(
            win, font=("Segoe UI", 12, "bold"), text_color=self.TEXT,
            text=tr("Single file up to 30 GB, over the Wi-Fi Direct link only.\n"
                    "Never touches the phone's internet exit; SHA-256 verified.",
                    "单文件最大 30 GB，仅走 Wi-Fi Direct 本地通道。\n不经手机外网出口；传输后校验 SHA-256。")
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

        # for it the moment it opens (the phone side asks the same way), and
        # the choice is persisted in settings.json so the next open starts
        # from it. "Receive" no longer asks: it just listens in that folder.
        recv_dir = {"path": load_app_settings().get("recvDir") or ""}

        def _refresh_recv_widgets():
            shown = recv_dir["path"] or tr(
                "not set (will ask when receiving)", "未设置（点接收时再询问）")
            recv_dir_label.configure(
                text=tr(f"Save to: {shown}", f"保存到：{shown}"))

        def _pick_recv_dir():
            path = filedialog.askdirectory(
                parent=win,
                title=tr("Choose the folder for incoming files",
                         "选择接收文件保存目录"),
                initialdir=(recv_dir["path"] if os.path.isdir(recv_dir["path"])
                            else os.path.expanduser("~/Downloads")))
            if path:
                recv_dir["path"] = path
                settings = load_app_settings()
                settings["recvDir"] = path
                save_app_settings(settings)
            _refresh_recv_widgets()

        recv_frame = ctk.CTkFrame(win, fg_color="transparent")
        recv_frame.pack(fill="x", padx=16, pady=(6, 0))
        recv_dir_label = ctk.CTkLabel(
            recv_frame, font=("Segoe UI", 10), text_color=self.TEXT,
            text=tr("Save to: not set", "保存到：未设置"), anchor="w")
        recv_dir_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            recv_frame, width=76,
            fg_color=self.STATUS_BG, hover_color=self.CARD, text_color=self.ACCENT,
            text=tr("Folder...", "目录…"), command=_pick_recv_dir).pack(side="right")
        _refresh_recv_widgets()

        status = ctk.CTkLabel(win, font=("Segoe UI", 10), text_color=self.MUTED,
                              text=tr("Idle", "空闲"))
        status.pack(padx=16, pady=(4, 2))
        bar = ctk.CTkProgressBar(win, width=430)
        bar.set(0)
        bar.pack(padx=16, pady=(0, 10))

        cancel_event = threading.Event()
        # port used to only open after "Receive", so a peer that tried to
        # PAIR (or send) while the dialog sat idle got ECONNREFUSED — the
        # phone then showed "cannot connect". PAIR handshakes do not
        # consume the receive slot, so this is free.
        arm_dir = os.path.join(os.environ.get("TEMP", "."), "linkhelm_ft")
        try:
            os.makedirs(arm_dir, exist_ok=True)
        except Exception:
            arm_dir = "."
        self._ft_server = file_transfer.ReceiverServer(
            local_ip, user, password,
            os.path.join(arm_dir, "placeholder"), log_fn=self.log)
        self._ft_server.start()

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
                parent=win, title=tr("Pick a file to send (<= 30 GB)", "选择要发送的文件（≤ 30 GB）"))
            if not path:
                return
            cancel_event.clear()

            def worker():
                # beta1.0.8 relay: an actual transfer is in flight — the
                # coordinator wakes for the duration and backs this sender
                # off per chunk when the link is under pressure.
                relay.RELAY.claim(relay.TRANSFER, "ft_sender")
                try:
                    file_transfer.send_file(
                        ip, user, password, path,
                        progress_cb=_progress, cancel_event=cancel_event, log_fn=self.log)
                    _set_status(tr("Sent and verified.", "已发送并校验。"), ok=True)
                    self.root.after(0, lambda: bar.set(1.0))
                except Exception as exc:
                    _set_status(str(exc), ok=False)
                finally:
                    relay.RELAY.release(relay.TRANSFER, "ft_sender")

            threading.Thread(target=worker, daemon=True).start()
            _set_status(tr("Sending...", "正在发送…"))

        def _do_receive():
            nonlocal local_ip
            # opened (or via "Folder..."). Only ask if it is somehow unset —
            # the old flow asked on every click, which the user explicitly
            # asked to move to dialog-open time.
            if not recv_dir["path"]:
                _pick_recv_dir()
                if not recv_dir["path"]:
                    _set_status(tr("Choose a save folder first.",
                                   "请先选择保存目录。"), ok=False)
                    return
            # replace the always-on pairing listener with the real receiver
            if self._ft_server:
                self._ft_server.stop()
            # time — the dialog may have been open across a Wi-Fi Direct
            # group rebuild, and binding a stale lease made the peer's
            # connect fail with ECONNREFUSED.
            peer = peer_entry.get().strip() or phone_ip
            live_ip = file_transfer.local_ip_toward(peer)
            if live_ip and live_ip != "0.0.0.0" and live_ip != local_ip:
                local_ip = live_ip
                local_label.configure(
                    text=tr(f"This device address (enter it on the other side): "
                            f"{local_ip}:{file_transfer.FT_PORT}",
                            f"本机地址（填到另一台设备）：{local_ip}:{file_transfer.FT_PORT}"))
            self._ft_server = file_transfer.ReceiverServer(
                local_ip, user, password, recv_dir["path"], log_fn=self.log)
            self._ft_server.start()
            bar.set(0)
            _set_status(tr("Waiting for the peer — files are saved to the chosen folder "
                           "under the sender's own name and format; a dropped link "
                           "keeps the partial, re-send the same file to resume "
                           "(stops after one file)...",
                           "等待对方发送——文件按发送方的原名和格式保存到所选目录；"
                           "断线会保留已传部分，重发同一文件即可续传"
                           "（完成一个文件后停止）…"))

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
        # so receiving is a single tap afterwards (cancelling keeps the
        # previous choice). The short delay lets the window paint first.
        win.after(200, _pick_recv_dir)

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
        # and status line instead of modal dialogs (silent start, no popups).
        try:
            p_port = int(self.phone_port.get().strip())
            l_port = int(self.local_port.get().strip())
        except ValueError:
            self.log("Invalid input: the port must be a number.")
            self._set_status(tr("Status: Invalid input (ports must be numbers)", "状态：输入有误（端口必须是数字）"), "error")
            return

        if not (0 < p_port < 65536) or not (0 < l_port < 65535 + 1):
            self.log("Invalid input: the port must be between 1 and 65535.")
            self._set_status(tr("Status: Invalid input (ports must be 1-65535)", "状态：输入有误（端口 1-65535）"), "error")
            return
        if p_port == l_port:
            self.log("Invalid input: the local download bridge port cannot equal the phone SOCKS5 port.")
            self._set_status(tr("Status: Invalid input (port conflict)", "状态：输入有误（端口冲突）"), "error")
            return

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
                # with and rebuild it (the engine reads the URL only at launch).
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
                            self.tun_mgr._restart_engine()
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

    def _open_store_accel_dialog(self):
        """beta1.0.63: dedicated accelerator page (like the file-transfer
        assistant). Runs the loopback-only Mihomo micro-kernel; never touches
        the system proxy, so it coexists with Clash Verge. The user pastes a
        subscription (or a clash:// one-click link), picks a node, and the
        kernel exposes a local proxy on 127.0.0.1 they opt into themselves.
        """
        win = ctk.CTkToplevel(self.root)
        win.title(tr("Store Boost (Clash)", "商店加速（Clash）"))
        win.geometry("520x610")
        win.transient(self.root)
        win.grab_set()

        pref = self.accel.load_pref()
        mode_var = ctk.StringVar(value=pref.get("mode", "rule"))
        # own pasted URL may contain a private token; restoring it into the
        # input at every launch is exactly the "预填我的token" the user has
        # forbidden. The preference file keeps the last URL (so start() can
        # re-use it internally if the user asks), but the UI never shows it
        # uninvited. DEFAULT_STORE_SUB_URL is "" since beta1.0.64.
        sub_var = ctk.StringVar(value="")
        # Some panels (xfltd observed) bind the subscription token to the
        # IP that first fetched it — fetching through the user's Clash
        # reproduces the exit route Clash Verge's own import used, which
        # is why "客户端可以正常导入，LinkHelm 却 403". Prefilled with the
        # user's Clash Verge mixed port.
        # the proxy IP right below (panels bind subscription tokens to the
        # fetching IP). Both prefilled with the user's Clash Verge values;
        # the subscription URL is NEVER prefilled (token stays private).
        clash_var = ctk.StringVar(
            value=pref.get("clashProxy") or "http://127.0.0.1:7897")
        clash_ip_var = ctk.StringVar(
            value=pref.get("clashIp") or "127.0.0.1")
        proxy_addr = ctk.StringVar(value="")

        def _set_status(text, ok=None):
            color = self.MUTED
            if ok is True:
                color = "#4ADE80"
            elif ok is False:
                color = "#F87171"
            win.after(0, lambda: status.configure(text=text, text_color=color))

        def _log(msg):
            win.after(0, lambda: (log_box.configure(state="normal"),
                                   log_box.insert("end", msg + "\n"),
                                   log_box.configure(state="disabled"),
                                   log_box.see("end")))

        _orig_log = self.log
        def _accel_log(msg):
            _log(msg)
            _orig_log(msg)
        self.accel.log = _accel_log

        def _refresh_hosts():
            _p, bom, _active, _size = store_accel.inspect_hosts()
            if bom > 0:
                hosts_label.configure(
                    text=tr("hosts 异常：发现 %d 个异常字符（BOM）" % bom,
                            "hosts anomaly: %d stray characters (BOM)" % bom),
                    text_color="#F87171")
                hosts_fix.configure(state="normal")
            else:
                hosts_label.configure(text=tr("hosts 文件正常", "hosts file OK"),
                                      text_color="#4ADE80")
                hosts_fix.configure(state="disabled")

        def _repair_hosts():
            ok, msg = store_accel.repair_hosts()
            _log("[accel] %s" % msg)
            _refresh_hosts()
            _set_status(msg, ok=ok)

        # ---- node picker: latency badges + select + auto-pick ----------
        _latencies = {}      # raw node name -> ms (or None)
        _shown = {}          # displayed label -> raw node name

        def _decorate(names):
            out = []
            for n in names:
                if n in _latencies:
                    ms = _latencies[n]
                    label = ("%s  (%d ms)" % (n, ms) if ms is not None
                             else "%s  (%s)" % (n, tr("timeout", "超时")))
                else:
                    label = n
                _shown[label] = n
                out.append(label)
            return out

        def _refresh_nodes():
            if not self.accel.is_running():
                return
            def _work():
                nodes = self.accel.list_nodes()
                cur = self.accel.current_node()
                def _apply():
                    _shown.clear()
                    if nodes:
                        labels = _decorate(nodes)
                        node_cb.configure(values=labels)
                        cur_label = ""
                        for label, raw in _shown.items():
                            if raw == cur:
                                cur_label = label
                                break
                        node_cb.set(cur_label or (labels[0] if labels else ""))
                    else:
                        node_cb.configure(
                            values=[tr("(no nodes yet)", "（暂无节点）")])
                win.after(0, _apply)
            threading.Thread(target=_work, daemon=True).start()

        _LAT_RE = re.compile(r"\s*\(\d+\s*ms\)$")
        _TO_RE = re.compile(r"\s*\(" + re.escape(tr("timeout", "超时")) + r"\)$")

        def _on_node_pick(value):
            node = _shown.get((value or "").strip(), (value or "").strip())
            node = _LAT_RE.sub("", node).strip()
            node = _TO_RE.sub("", node).strip()
            if not node or node.startswith("(") or node.startswith("\uFF08"):
                return
            if not self.accel.is_running():
                return
            def _work():
                ok, msg = self.accel.select_node(node)
                if ok:
                    self.accel.save_pref(node=node)
                    win.after(0, lambda: _set_status(
                        tr("Node: ", "当前节点：") + node, ok=True))
                else:
                    win.after(0, lambda: _set_status(
                        tr("Node switch failed: ", "切换节点失败：") + str(msg),
                        ok=False))
            threading.Thread(target=_work, daemon=True).start()

        def _latency_test():
            if not self.accel.is_running():
                _set_status(tr("Start the accelerator first.", "请先启动加速。"),
                            ok=False)
                return
            lat_btn.configure(state="disabled")
            auto_btn.configure(state="disabled")
            _set_status(tr("Testing latency…", "正在测延迟…"))
            def _work():
                res = self.accel.test_delays()
                def _apply():
                    _latencies.clear()
                    _latencies.update(res)
                    good = {n: m for n, m in res.items() if m is not None}
                    if good:
                        best = min(good, key=good.get)
                        for n, m in sorted(
                                res.items(),
                                key=lambda kv: (kv[1] is None, kv[1])):
                            _log("[delay] %s: %s" % (
                                n, ("%d ms" % m) if m is not None
                                else tr("timeout", "超时")))
                        _set_status(
                            tr("Fastest: %s (%d ms)", "最快：%s（%d ms）")
                            % (best, good[best]), ok=True)
                    else:
                        _set_status(
                            tr("No node answered the latency test.",
                               "没有节点响应延迟测试。"), ok=False)
                    _refresh_nodes()
                    lat_btn.configure(state="normal")
                    auto_btn.configure(state="normal")
                win.after(0, _apply)
            threading.Thread(target=_work, daemon=True).start()

        def _auto_pick():
            if not self.accel.is_running():
                _set_status(tr("Start the accelerator first.", "请先启动加速。"),
                            ok=False)
                return
            lat_btn.configure(state="disabled")
            auto_btn.configure(state="disabled")
            _set_status(tr("Auto-selecting fastest node…",
                           "正在自动选择最快节点…"))
            def _work():
                best, ms, res = self.accel.auto_select()
                def _apply():
                    _latencies.clear()
                    _latencies.update(res)
                    if best:
                        _log("[accel] auto-selected %s (%d ms)" % (best, ms))
                        _set_status(
                            tr("Auto-selected: %s (%d ms)",
                               "已自动选择：%s（%d ms）") % (best, ms), ok=True)
                    else:
                        _set_status(
                            tr("Auto-select failed: no node answered.",
                               "自动选择失败：没有节点响应。"), ok=False)
                    _refresh_nodes()
                    lat_btn.configure(state="normal")
                    auto_btn.configure(state="normal")
                win.after(0, _apply)
            threading.Thread(target=_work, daemon=True).start()

        def _do_start():
            sub = (sub_var.get() or "").strip()
            if not sub:
                # must never sit on screen uninvited) — fall back to the
                # subscription URL saved from a previous start so the
                # one-click flow still works without pasting every time.
                sub = (pref.get("subUrl") or "").strip()
            if not sub:
                _set_status(tr("Paste a subscription link first.",
                               "请先粘贴订阅链接。"), ok=False)
                return
            mode = mode_var.get()
            cp = (clash_var.get() or "").strip()
            cip = (clash_ip_var.get() or "").strip()
            if not cp and cip:
                # URL row empty -> derive it from the IP row (default port).
                if "://" not in cip and ":" not in cip:
                    cip = cip + ":7897"
                cp = cip if "://" in cip else "http://" + cip
            start_btn.configure(state="disabled")
            _set_status(tr("Starting… (download subscription, launch kernel)",
                           "正在启动…（下载订阅、启动内核）"))
            def _worker():
                ok, msg = self.accel.start(sub, mode=mode, clash_proxy=cp)
                if ok:
                    # beta1.0.8 relay: the coordinator wakes only now — the
                    # store booster actually started. Loopback-only lane;
                    # the relay refuses any system-proxy/route writes here.
                    relay.RELAY.claim(relay.STORE, "store_boost")
                    try:
                        self.accel.save_pref(clashIp=cip)
                    except Exception:
                        pass
                    proxy_addr.set("127.0.0.1:%d" % self.accel.mixed_port)
                    win.after(0, lambda: proxy_label.configure(
                        text=tr("Local proxy: ", "本地代理：") + proxy_addr.get(),
                        text_color="#4ADE80"))
                    win.after(0, lambda: _set_status(self.accel.status_text(),
                                                    ok=True))
                    win.after(0, lambda: start_btn.configure(state="disabled"))
                    win.after(0, lambda: stop_btn.configure(state="normal"))
                    win.after(2500, _refresh_nodes)
                else:
                    win.after(0, lambda: _set_status(
                        tr("Start failed: ", "启动失败：") + msg, ok=False))
                    win.after(0, lambda: start_btn.configure(state="normal"))
                win.after(0, lambda: log_box.see("end"))
            threading.Thread(target=_worker, daemon=True).start()

        def _do_stop():
            self.accel.stop()
            # beta1.0.8 relay: both optional features may now be idle —
            # the coordinator returns to silent standby automatically.
            relay.RELAY.release(relay.STORE, "store_boost")
            _set_status(tr("Stopped.", "已停止。"))
            start_btn.configure(state="normal")
            stop_btn.configure(state="disabled")
            proxy_label.configure(
                text=tr("Local proxy: not started", "本地代理：未启动"),
                text_color=self.MUTED)
            proxy_addr.set("")

        def _refresh_status():
            if self.accel.is_running():
                proxy_addr.set("127.0.0.1:%d" % self.accel.mixed_port)
                proxy_label.configure(
                    text=tr("Local proxy: ", "本地代理：") + proxy_addr.get(),
                    text_color="#4ADE80")
                start_btn.configure(state="disabled")
                stop_btn.configure(state="normal")
                _set_status(self.accel.status_text(), ok=True)
                _refresh_nodes()
            else:
                start_btn.configure(state="normal")
                stop_btn.configure(state="disabled")

        def _close():
            self.accel.log = _orig_log
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _close)

        # ---- header ----
        ctk.CTkLabel(
            win, font=("Segoe UI", 12, "bold"), text_color=self.TEXT,
            text=tr("Built-in open-source Clash core — loopback micro-kernel, "
                    "coexists with Clash Verge.",
                    "内置开源 Clash 内核——仅本地回环微内核，与 Clash Verge 共存。")
        ).pack(padx=16, pady=(14, 4))

        # ---- subscription input ----
        sub_frame = ctk.CTkFrame(win, fg_color="transparent")
        sub_frame.pack(fill="x", padx=16, pady=(2, 0))
        sub_entry = ctk.CTkEntry(
            sub_frame, height=33, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 11),
            textvariable=sub_var,
            placeholder_text=tr(
                "clash:// link / subscription URL / .yaml path / paste YAML",
                "clash:// 链接 / 订阅地址 / .yaml 路径 / 直接粘贴配置"))
        sub_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            sub_frame, text=tr("Paste", "粘贴"), width=64, height=33,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.STATUS_BG, hover_color=self.CARD,
            text_color=self.ACCENT,
            command=lambda: sub_var.set(win.clipboard_get() or "")).pack(side="right")

        # ---- mode + xfltd guidance ----
        opt_frame = ctk.CTkFrame(win, fg_color="transparent")
        opt_frame.pack(fill="x", padx=16, pady=(8, 0))
        mode_seg = ctk.CTkSegmentedButton(
            opt_frame,
            values=[tr("Rule (stores only)", "规则（仅商店）"),
                    tr("Global (all traffic)", "全局（全部流量）")],
            height=28, font=("Segoe UI", 10),
            command=lambda v: mode_var.set(
                "global" if ("Global" in (v or "") or "\u5168\u5c40" in (v or ""))
                else "rule"))
        mode_seg.pack(side="left")
        mode_seg.set(tr("Rule (stores only)", "规则（仅商店）")
                     if mode_var.get() != "global"
                     else tr("Global (all traffic)", "全局（全部流量）"))
        ctk.CTkLabel(
            opt_frame, font=("Segoe UI", 9), text_color=self.MUTED,
            text=tr("From xfltd tutorial: copy the subscription link, or use the "
                    "panel's 'one-click import to Clash Verge' clash:// link.",
                    "参考 xfltd 教程：复制订阅地址，或直接使用面板「一键导入 Clash "
                    "Verge」的 clash:// 链接。")).pack(side="left", padx=(10, 0))

        # ---- node picker ----
        node_frame = ctk.CTkFrame(win, fg_color="transparent")
        node_frame.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(node_frame, text=tr("Node", "节点"),
                     font=("Segoe UI", 10), text_color=self.MUTED
                     ).pack(side="left", padx=(0, 8))
        node_cb = ctk.CTkComboBox(
            node_frame, height=28, corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.INPUT_BG, border_color=self.INPUT_BORDER,
            button_color=self.INPUT_BORDER, button_hover_color=self.BORDER,
            dropdown_font=("Segoe UI", 10),
            values=[tr("(start the boost to load nodes)", "（启动加速后加载节点）")],
            command=_on_node_pick)
        node_cb.pack(side="left", fill="x", expand=True, padx=(0, 8))
        auto_btn = ctk.CTkButton(
            node_frame, text=tr("Auto", "自动选择"), width=70, height=28,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.CARD, hover_color=self.BORDER, text_color=self.ACCENT,
            border_width=1, border_color=self.BORDER,
            command=_auto_pick)
        auto_btn.pack(side="right")
        lat_btn = ctk.CTkButton(
            node_frame, text=tr("Delay", "延迟测试"), width=70, height=28,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.CARD, hover_color=self.BORDER, text_color=self.ACCENT,
            border_width=1, border_color=self.BORDER,
            command=_latency_test)
        lat_btn.pack(side="right", padx=(0, 6))
        ctk.CTkButton(
            node_frame, text=tr("Refresh", "刷新"), width=64, height=28,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.CARD, hover_color=self.BORDER, text_color=self.ACCENT,
            border_width=1, border_color=self.BORDER,
            command=_refresh_nodes).pack(side="right", padx=(0, 6))

        # ---- beta1.0.7: proxy URL row + proxy IP row ----------------------
        # The subscription is fetched THROUGH the proxy URL when a panel
        # binds the token to the IP that first fetched it — that is why
        # Clash Verge's own import succeeds while a direct fetch 403s. Both
        # rows are editable; the URL row wins when both are filled.
        clash_frame = ctk.CTkFrame(win, fg_color="transparent")
        clash_frame.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(
            clash_frame, font=("Segoe UI", 10), text_color=self.MUTED,
            text=tr("Proxy URL", "代理网址")
        ).pack(side="left", padx=(0, 8))
        clash_entry = ctk.CTkEntry(
            clash_frame, height=28, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 10),
            textvariable=clash_var, placeholder_text="http://127.0.0.1:7897")
        clash_entry.pack(side="left", fill="x", expand=True)

        clash_ip_frame = ctk.CTkFrame(win, fg_color="transparent")
        clash_ip_frame.pack(fill="x", padx=16, pady=(6, 0))
        ctk.CTkLabel(
            clash_ip_frame, font=("Segoe UI", 10), text_color=self.MUTED,
            text=tr("Proxy IP", "代理 IP")
        ).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            clash_ip_frame, height=28, corner_radius=7, fg_color=self.INPUT_BG,
            border_color=self.INPUT_BORDER, font=("Segoe UI", 10),
            textvariable=clash_ip_var, placeholder_text="127.0.0.1"
        ).pack(side="left", fill="x", expand=True)

        # ---- local proxy address + copy ----
        proxy_frame = ctk.CTkFrame(win, fg_color="transparent")
        proxy_frame.pack(fill="x", padx=16, pady=(8, 0))
        proxy_label = ctk.CTkLabel(
            proxy_frame, font=("Segoe UI", 10), text_color=self.MUTED,
            text=tr("Local proxy: not started", "本地代理：未启动"))
        proxy_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            proxy_frame, text=tr("Copy", "复制"), width=64, height=26,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.STATUS_BG, hover_color=self.CARD,
            text_color=self.ACCENT,
            command=lambda: (win.clipboard_clear(),
                            win.clipboard_append(proxy_addr.get() or ""))
            ).pack(side="right")

        # ---- hosts integrity (电脑管家 "host 异常" fix) ----
        hosts_frame = ctk.CTkFrame(win, fg_color="transparent")
        hosts_frame.pack(fill="x", padx=16, pady=(8, 0))
        hosts_label = ctk.CTkLabel(
            hosts_frame, font=("Segoe UI", 10), text_color=self.MUTED, text="")
        hosts_label.pack(side="left", fill="x", expand=True)
        hosts_fix = ctk.CTkButton(
            hosts_frame, text=tr("Repair hosts", "修复 hosts"), width=96, height=26,
            corner_radius=7, font=("Segoe UI", 10),
            fg_color=self.CARD, hover_color=self.BORDER, text_color=self.ACCENT,
            border_width=1, border_color=self.BORDER, command=_repair_hosts)
        hosts_fix.pack(side="right")

        # ---- start / stop ----
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(10, 0))
        start_btn = ctk.CTkButton(
            btn_row, text=tr("\u25b6  Start boost", "\u25b6  启动加速"), height=33,
            corner_radius=7, fg_color=self.ACCENT, hover_color="#005AAB",
            font=("Segoe UI", 11, "bold"), command=_do_start)
        start_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
        stop_btn = ctk.CTkButton(
            btn_row, text=tr("Stop", "停止"), height=33, corner_radius=7,
            state="disabled", fg_color=self.INPUT_BG, border_width=1,
            border_color=self.BORDER, text_color=self.TEXT,
            hover_color=self.STATUS_BG, font=("Segoe UI", 11),
            command=_do_stop)
        stop_btn.pack(side="left", expand=True, fill="x")

        status = ctk.CTkLabel(
            win, font=("Segoe UI", 10), text_color=self.MUTED,
            text=tr("Idle. Paste a subscription, then start.",
                    "未运行。粘贴订阅后启动加速。"))
        status.pack(padx=16, pady=(6, 2))

        log_box = ctk.CTkTextbox(win, font=("Consolas", 9), height=150, wrap="word")
        log_box.pack(fill="x", padx=16, pady=(4, 12))
        log_box.configure(state="disabled")

        _refresh_hosts()
        _refresh_status()
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
                    tr("The built-in lwIP (hev-socks5-tunnel) / Wintun components were not found.\n\n",
                       "未找到内置的 lwIP（hev-socks5-tunnel）/ Wintun 组件。\n\n")
                    + tr(f"A locked build of lwIP (hev-socks5-tunnel v{LWIP_ENGINE_VERSION}, "
                         f"Wintun v{WINTUN_VERSION}) can be downloaded via the phone proxy. ",
                         f"可通过手机代理下载锁定版本（hev-socks5-tunnel v{LWIP_ENGINE_VERSION}，"
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
        self._socks_user = s_user
        self._socks_pass = s_pass
        try:
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

    # invisible in a --windowed build — and aborts whatever that callback still
    # had to do (status colour, progress spinner, follow-up scheduling). Route
    # it into the app log so a report is actionable and keep the UI alive.
    def _tk_report_exception(exc, val, tb):
        try:
            app.log(f"UI: {exc.__name__}: {val}")
        except Exception:
            pass
        try:
            traceback.print_exception(exc, val, tb)
        except Exception:
            pass

    root.report_callback_exception = _tk_report_exception

    # Tray: the window is a control surface; the tray thread only enqueues
    # actions, every Tk call happens on the GUI thread via root.after.
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
        # path (taskkill/powershell up to ~7s on the Tk main thread) was the
        # reported "tray quit -> window not responding" freeze. The window
        # hides immediately; cleanup completes in the background, then the
        # process force-exits. One-shot guard unchanged (P0).
        if teardown_state["started"]:
            return
        teardown_state["started"] = True
        try:
            root.withdraw()  # instant visual feedback; never block the UI
        except Exception:
            pass

        def _background_teardown():
            try:
                if tray_icon_holder["icon"] is not None:
                    try:
                        tray_icon_holder["icon"].stop()
                    except Exception:
                        pass
                if app.tun_mgr or app.ics_mgr.needs_cleanup:
                    if not app._do_stop(is_closing=True):
                        app.log("Could not restore Windows sharing settings; "
                                "forcing exit anyway (network state was swept).")
                try:
                    app.accel.shutdown()
                except Exception:
                    pass
                _kill_engine_procs_all()
            finally:
                root.destroy()
                os._exit(0)

        threading.Thread(target=_background_teardown, daemon=True).start()

    def on_close_request():
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
    # the /1 routes and ICS until both break ("clash"). A named OS mutex makes
    # any second launch exit cleanly with a clear message instead. Checked
    # BEFORE the UAC relaunch so a duplicate start never prompts for admin.
    #
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