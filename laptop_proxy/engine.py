"""beta1.0.4: NetworkEngine abstraction (Windows底层迁移).

Windows 用户态的 IP/TCP/UDP 协议栈由独立原生引擎承接，Python 只做编排
（生成配置、拉起/停止进程、互斥持有 Wintun、隔离每用户配置、停机清理）。
协议栈本身绝不写进 Python —— 这正是任务书要求的"lwIP 用 C++ 重新编译"：

  - LegacyEngine : 现有 tun2socks (Go) 二进制，行为完全不变（默认）。
  - LwipEngine   : 原生 C/C++ 的 lwIP 引擎 (hev-socks5-tunnel)，由 MSYS2
                   编译出 hev-socks5-tunnel.exe（见 build-lwip.ps1）。本模块
                   只负责写配置、拉起该原生进程、回收资源。

EngineManager 同时刻只允许一个引擎持有 Wintun 会话，禁止新旧双内核抢包。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from abc import ABC, abstractmethod
from typing import Tuple

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
# 原生 lwIP 引擎二进制（由 build-lwip.ps1 在 MSYS2 下编译产出）。
LWIP_EXE = os.path.join(BIN_DIR, "engine", "hev-socks5-tunnel.exe")
# 每用户配置目录（受当前用户权限限制，凭据不落日志、不乱放到安装目录）。
ENGINE_CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "LinkHelm", "engine",
)
ADAPTER_NAME = "LinkHelm"


def _parse_proxy_url(proxy_url: str) -> Tuple[str, int, str, str]:
    """socks5://user:pass@host:port -> (host, port, user, pass)."""
    p = urllib.parse.urlparse(proxy_url)
    host = p.hostname or "192.168.49.1"
    port = p.port or 8080
    user = urllib.parse.unquote(p.username or "")
    pw = urllib.parse.unquote(p.password or "")
    return host, port, user, pw


class NetworkEngine(ABC):
    name = "abstract"

    @abstractmethod
    def build_launch(self, proxy_url: str, mtu: int = 1420) -> Tuple[str, list, str]:
        """Return (exe_path, args, cwd) for subprocess.Popen."""
        raise NotImplementedError

    @abstractmethod
    def configured(self) -> bool:
        """Whether this engine's native binary is present and runnable."""
        raise NotImplementedError

    def teardown(self) -> None:
        """Best-effort cleanup of engine-owned resources on stop."""
        return None


class LegacyEngine(NetworkEngine):
    name = "legacy"

    def __init__(self, t2s_exe: str):
        self.t2s_exe = t2s_exe

    def build_launch(self, proxy_url: str, mtu: int = 1420):
        # Imported lazily to avoid a circular import at module load.
        from main import tun2socks_cmd  # noqa: WPS433
        cmd = tun2socks_cmd(self.t2s_exe, proxy_url)
        return cmd[0], list(cmd[1:]), BIN_DIR

    def configured(self) -> bool:
        return os.path.isfile(self.t2s_exe)


class LwipEngine(NetworkEngine):
    name = "lwip"
    """Native C/C++ lwIP engine (hev-socks5-tunnel). Python only orchestrates."""

    def build_launch(self, proxy_url: str, mtu: int = 1420):
        os.makedirs(ENGINE_CONFIG_DIR, exist_ok=True)
        host, port, user, pw = _parse_proxy_url(proxy_url)
        # 凭据只进每用户配置文件，不进日志、不进命令行参数。
        config = {
            "log-level": "info",
            "log-file": os.path.join(ENGINE_CONFIG_DIR, "lwip.log"),
            "pid-file": os.path.join(ENGINE_CONFIG_DIR, "lwip.pid"),
            "socks5": {
                "server": host,
                "port": port,
                "username": user,
                "password": pw,
                "mtu": mtu,
            },
            "wintun": {
                "name": ADAPTER_NAME,
                "mtu": mtu,
            },
        }
        cfg_path = os.path.join(ENGINE_CONFIG_DIR, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        return LWIP_EXE, ["--config", cfg_path], ENGINE_CONFIG_DIR

    def configured(self) -> bool:
        return os.path.isfile(LWIP_EXE)

    def teardown(self) -> None:
        # hev writes a pid file; a stray one would block the next launch.
        for f in ("lwip.pid",):
            try:
                os.remove(os.path.join(ENGINE_CONFIG_DIR, f))
            except OSError:
                pass


class EngineManager:
    """Selects and guards the active engine; enforces single Wintun owner.

    Default engine is now **lwIP** (the native C/C++ kernel). tun2socks v2.7.0
    is kept ONLY as a backup binary on disk and is used automatically if the
    lwIP binary has not been built yet (or is missing) — so the app still
    connects before the C/C++ engine is compiled. The two never run at the same
    time (single Wintun owner), which is exactly why we must not keep tun2socks
    as an *active* default: two kernels competing for the same adapter "fight".
    """

    def __init__(self, mode: str = "lwip"):
        self._lock = threading.Lock()
        self._active: NetworkEngine | None = None
        self._mode = mode
        self._t2s_exe: str | None = None

    def set_legacy_binary(self, t2s_exe: str) -> None:
        self._t2s_exe = t2s_exe

    def _make(self, mode: str):
        if mode == "lwip":
            e = LwipEngine()
            if e.configured():
                return e, None
            # 原生二进制尚未编译/缺失：回退到备份的 tun2socks（仍单例互斥，
            # 不会与 lwIP 同时持有 Wintun）。构建出 hev-socks5-tunnel.exe 后
            # 下次启动自动切回 lwIP，无需改任何设置。
            return LegacyEngine(self._t2s_exe), (
                "lwIP 原生引擎未就绪，已回退到备份 tun2socks（请用 ci/build-lwip.ps1 编译 hev-socks5-tunnel.exe）"
            )
        return LegacyEngine(self._t2s_exe), None

    def build_launch(self, proxy_url: str, mtu: int = 1420):
        with self._lock:
            engine, note = self._make(self._mode)
            self._active = engine
            return engine.build_launch(proxy_url, mtu=mtu), engine.name, note

    def teardown(self) -> None:
        with self._lock:
            if self._active is not None:
                self._active.teardown()
                self._active = None

    def active_name(self) -> str:
        with self._lock:
            return self._active.name if self._active else "none"


# 全局单例：默认 lwIP；原生二进制缺失时自动回退 tun2socks 备份。
# 可用环境变量 LINKHELM_ENGINE=legacy 强制走备份（仅排障用）。
ENGINE_MANAGER = EngineManager(mode=os.environ.get("LINKHELM_ENGINE", "lwip"))
