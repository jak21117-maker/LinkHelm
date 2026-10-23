"""beta1.0.5: NetworkEngine abstraction (Windows底层迁移 — lwIP 完全接管).

Windows 用户态的 IP/TCP/UDP 协议栈由独立原生引擎承接，Python 只做编排
（生成配置、拉起/停止进程、互斥持有 Wintun、隔离每用户配置、停机清理）。
协议栈本身绝不写进 Python —— 这正是任务书要求的"lwIP 用 C/C++ 重新编译"：

  - LwipEngine : 原生 C/C++ 的 lwIP 引擎 (hev-socks5-tunnel)，由官方 Windows
                  构建 hev-socks5-tunnel.exe（见 DEPENDENCY_LOCK.md tag 2.19.1）
                  承接 IP/TCP/UDP/DNS —— lwIP 内部以 PCB（协议控制块）统一处理
                  TCP、UDP 与 DNS 中继，不再有旧版"多并发 DNS/UDP/TCP"各自开
                  连接互相抢链路的实现。手机（Android Host）侧握手同样由该内核
                  的 PCB 栈完成：每个 TCP 会话走 SOCKS5 TCP CONNECT，UDP/DNS 走
                  SOCKS5 UDP ASSOCIATE，全部在 lwIP 内闭环。

beta1.0.5 起 tun2socks 已彻底移除（不再保留备份二进制、不再有 LegacyEngine）。
旧内核若遗留虚拟网卡，由 main.py 在首次/每次启动前统一清理（见
_cleanup_stale_tun_adapters），确保只保留单一干净的 LinkHelm-IwIP 适配器。

EngineManager 同时刻只允许一个引擎持有 Wintun 会话，禁止新旧双内核抢包。
"""

from __future__ import annotations

import os
import sys
import threading
import urllib.parse
from abc import ABC, abstractmethod
from typing import Tuple

if getattr(sys, "frozen", False):
    # PyInstaller: sys._MEIPASS == <app>/_internal in the onedir layout (the
    # extraction dir in onefile). The engine trio ships at
    # <app>/_internal/engine/ (LaptopProxy.spec binaries entry, target 'engine'),
    # so _MEIPASS is the only reliable anchor: for a PYZ-embedded module,
    # __file__ is a virtual arcname path that need NOT exist on disk — engine.py
    # itself lives inside the PYZ in the exe, not as _internal/engine.py.
    BIN_DIR = sys._MEIPASS
else:
    BIN_DIR = os.path.dirname(os.path.abspath(__file__))
# 原生 lwIP 引擎二进制（官方 Windows 构建 hev-socks5-tunnel-win64.zip，tag 2.19.1）。
# 路径经 engine_bin_dir()/engine_exe_path() 在【调用时】解析（非导入时），
# 测试可重定向 BIN_DIR；冻结包内三件套固定位于 <app>/_internal/engine/。
ENGINE_SUBDIR = "engine"
LWIP_EXE_NAME = "hev-socks5-tunnel.exe"
WINTUN_DLL_NAME = "wintun.dll"
MSYS_DLL_NAME = "msys-2.0.dll"


def engine_bin_dir() -> str:
    """Directory holding the bundled native engine trio (call-time resolution)."""
    return os.path.join(BIN_DIR, ENGINE_SUBDIR)


def engine_exe_path() -> str:
    """Absolute path of hev-socks5-tunnel.exe inside the bundled engine/ dir."""
    return os.path.join(engine_bin_dir(), LWIP_EXE_NAME)


def engine_trio_paths() -> Tuple[str, str, str]:
    """(exe, wintun.dll, msys-2.0.dll) — the integrity-locked engine trio."""
    d = engine_bin_dir()
    return (
        os.path.join(d, LWIP_EXE_NAME),
        os.path.join(d, WINTUN_DLL_NAME),
        os.path.join(d, MSYS_DLL_NAME),
    )


# Backwards-compatible constant; prefer engine_exe_path() for new code.
LWIP_EXE = engine_exe_path()
# 每用户配置目录（受当前用户权限限制，凭据不落日志、不乱放到安装目录）。
ENGINE_CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "LinkHelm", "engine",
)
# beta1.0.5: 新适配器名。与旧 tun2socks 时代的 "LinkHelm"/"LinkHelm 2/3/4"
#  deliberately 区分开——启动清理（_cleanup_stale_tun_adapters）只扫旧模式，
# 永不误删本适配器，从而"先清旧 → 建新 → 后续复用同一适配器"不会互相打架。
ADAPTER_NAME = "LinkHelm-IwIP"
# Fixed Wintun adapter GUID. hev-socks5-tunnel passes it to WintunCreateAdapter,
# which REOPENS the existing adapter when the GUID matches — so every run reuses
# the SAME adapter identity and Windows never piles up "LinkHelm-IwIP 2/3/4".
# The first launch clears pre-existing legacy adapters (tun2socks leftovers
# named LinkHelm*/LaptopProxyVPN*) and from then on this single adapter is kept.
# Format: hyphens, no braces (Hev's `tunnel.guid`, parsed by hev_wintun_parse_guid
# which requires exactly the 36-char 8-4-4-4-12 form below).
ADAPTER_GUID = "F4E4A1B2-9C3D-4E5F-8A7B-6C5D4E3F2A1B"
# The adapter's own IPv4. Hev assigns it to the Wintun adapter (as /32) at every
# start; main.py then layers the routable 10.0.0.2/24 with gateway 10.0.0.1 on
# top via netsh (see OUR_TUN_GATEWAY / _configure_adapter_if_needed). Keeping
# this equal to OUR_TUN_GATEWAY means the /1 routes are valid even before the
# netsh step lands.
ADAPTER_IPV4 = "10.0.0.1"
ADAPTER_IPV6 = "fc00::1"


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


class LwipEngine(NetworkEngine):
    """Native C/C++ lwIP engine (hev-socks5-tunnel). Python only orchestrates."""

    name = "lwip"

    def build_launch(self, proxy_url: str, mtu: int = 1420):
        os.makedirs(ENGINE_CONFIG_DIR, exist_ok=True)
        host, port, user, pw = _parse_proxy_url(proxy_url)

        def q(s) -> str:
            # YAML 单引号字面量：内部 ' 翻倍；反斜杠保持原样（Windows 路径安全）。
            return "'" + str(s).replace("'", "''") + "'"

        # 凭据只进每用户配置文件（YAML 是 Hev 的原生配置格式），不进日志、
        # 不进命令行参数。键名/结构严格按 Hev 的 conf schema（src/hev-config.c）：
        #   tunnel.* 开 Wintun 适配器（name=LinkHelm-IwIP，guid 固定 → 复用同一
        #           适配器，不再出现 "LinkHelm-IwIP 2/3/4"），socks5.* 指上游。
        #   misc.*   连接超时/读写超时/会话数/日志/pid —— 注意：这些键只被
        #           `misc:` 段识别（hev_config_parse_misc），误放到 `socks5:`
        #           会被解析器静默忽略（默认值继续生效）。
        # Hev 用 PCB 栈统一处理 TCP/UDP/DNS —— udp:'udp' 开启 UDP 中继，
        # connect-timeout 拉长到 25s 以容纳手机 Wi-Fi Direct 拨号 + 队列（任务书
        # 提示 15s 拨号 + 1.5s 队列，上游默认 10s 太短会误判握手失败）。
        lines = [
            "tunnel:",
            f"  name: {ADAPTER_NAME}",
            f"  guid: {ADAPTER_GUID}",
            f"  mtu: {int(mtu)}",
            f"  ipv4: {ADAPTER_IPV4}",
            f"  ipv6: '{ADAPTER_IPV6}'",
            "  icmp: 'off'",
            "socks5:",
            f"  address: {q(host)}",
            f"  port: {int(port)}",
            "  udp: 'udp'",
            # beta1.0.52: pin the UDP relay endpoint to the phone's own address.
            # Some hosts reply UDP ASSOCIATE with BND.ADDR=0.0.0.0 (legal per RFC,
            # "same as the server"); hev then connect()s its UDP socket to 0.0.0.0,
            # which Windows rejects with WSAEADDRNOTAVAIL (10049) and every
            # tunneled UDP session (all DNS!) dies. udp-address overrides ONLY the
            # host part of the relay endpoint (the relay port from the reply is
            # kept), so the data plane is correct regardless of what the host
            # advertises. See src/hev-socks5-session-udp.c set_upstream_addr.
            f"  udp-address: {q(host)}",
        ]
        if user:
            lines.append(f"  username: {q(user)}")
        if pw:
            lines.append(f"  password: {q(pw)}")
        lines += [
            "misc:",
            "  connect-timeout: 25000",
            "  tcp-read-write-timeout: 300000",
            "  udp-read-write-timeout: 60000",
            "  max-session-count: 0",
            f"  log-file: {q(os.path.join(ENGINE_CONFIG_DIR, 'lwip.log'))}",
            "  log-level: info",
            # NO misc.pid-file on purpose: hev-main.c does
            # `if (pid_file) run_as_daemon(pid_file)` — a pid-file makes the
            # engine FORK into a detached daemon whose parent exits 0 at once.
            # main.py owns the engine lifecycle via Popen + crash-watchdog, so
            # the engine MUST stay in the foreground as our direct child.
        ]
        cfg_path = os.path.join(ENGINE_CONFIG_DIR, "main.yml")
        with open(cfg_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        # Hev 用位置参数接收配置路径（hev-socks5-tunnel <config.yml>）。
        return engine_exe_path(), [cfg_path], ENGINE_CONFIG_DIR

    def configured(self) -> bool:
        return os.path.isfile(engine_exe_path())

    def teardown(self) -> None:
        # No pid file is configured (see build_launch) — hev runs in the
        # foreground as our child, so there is nothing to reap here. A stale
        # lwip.pid from a pre-beta1.0.5 config is swept anyway, just in case.
        for f in ("lwip.pid",):
            try:
                os.remove(os.path.join(ENGINE_CONFIG_DIR, f))
            except OSError:
                pass


class EngineManager:
    """Selects and guards the active engine; enforces single Wintun owner.

    beta1.0.5 起仅保留 lwIP 单一引擎：tun2socks 已彻底移除，不再有备份回退、
    不再有双内核抢包问题。EngineManager 仍保留单例互斥语义，便于将来扩展，但
    当前 build_launch 永远返回 LwipEngine。
    """

    def __init__(self, mode: str = "lwip"):
        self._lock = threading.Lock()
        self._active: NetworkEngine | None = None
        self._mode = mode

    def _make(self, mode: str):
        engine = LwipEngine()
        if engine.configured():
            return engine, None
        # 原生二进制缺失（安装包未正确携带 engine/hev-socks5-tunnel.exe）：
        # 无法启动，返回明确的错误提示而非静默回退到不存在的旧内核。
        return engine, (
            "lwIP 原生引擎 (engine/hev-socks5-tunnel.exe) 未找到，"
            "请确认安装包完整（engine/ 目录下应包含 hev-socks5-tunnel.exe、"
            "wintun.dll、msys-2.0.dll）。"
        )

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


# 全局单例：固定 lwIP。可用环境变量 LINKHELM_ENGINE 仅作排障日志标记，不再切换内核。
ENGINE_MANAGER = EngineManager(mode=os.environ.get("LINKHELM_ENGINE", "lwip"))
