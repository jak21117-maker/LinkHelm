# DEPENDENCY_LOCK — External Binary Lock Manifest

Builder verification date: 2026-10-15. All hashes are SHA-256 computed on the **complete ZIP files** downloaded from official distribution points.

| Component | Version | Official source URL | SHA-256 (file) |
|---|---|---|---|
| HevSocks5Tunnel (Windows win64 exe) | 2.19.1 | https://github.com/heiher/hev-socks5-tunnel/releases/download/2.19.1/hev-socks5-tunnel-win64.zip | exe `242557dc4ada5a45bcc02d98a558e86dd29025efdf7960b8e302bfc2ad1901c5` |
| Wintun | 0.14.1 | https://www.wintun.net/builds/wintun-0.14.1.zip | `07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51` |
| HevSocks5Tunnel (Android AAR) | 2.19.1 | https://github.com/heiher/hev-socks5-tunnel/releases/download/2.19.1/hev-socks5-tunnel.aar | `107958f7a07051386248ba8dba778b63d8dcefdfb2c3665b0ee9a8c93de5f6ca` (605512 B) |

## Bundled binary notes

- **beta1.0.5: tun2socks 已彻底移除**，Windows 与 Android 内核统一由 **HevSocks5Tunnel（lwIP + Wintun）** 承接。不再保留 tun2socks 备份二进制、不再有 legacy 回退、不再有运行时下载。
- Windows 原生引擎随安装包 `laptop_proxy/engine/` 目录提供（三件套，均由官方 win64 zip 解出的同一对象）：
  - `hev-socks5-tunnel.exe` — SHA-256 `242557dc4ada5a45bcc02d98a558e86dd29025efdf7960b8e302bfc2ad1901c5`
  - `wintun.dll` — SHA-256 `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce`
  - `msys-2.0.dll` — SHA-256 `1410599ee2efcede0869abec8910398d688755952e59cf7983b15cac6de78201`
- 二进制完整性在运行时再次 SHA-256 校验（`main.py` 的 `EXPECTED_BIN_SHA256`），程序以管理员权限运行，防止被替换的缓存二进制提权。
- 不再有 tun2socks `--version` 校验、不再有 `tun2socks_*.exe` 缓存清理、不再有下载回退路径——安装包必须完整携带 `engine/` 三件套。

## Exact ZIP members used

- Windows engine: `hev-socks5-tunnel-win64.zip` → `hev-socks5-tunnel.exe` + `wintun.dll` + `msys-2.0.dll`（三件套全部放入 `laptop_proxy/engine/`，哈希见上表）
- Android engine: `hev-socks5-tunnel.aar` → `classes.jar`（JNI 类 `hev.htproxy.TProxyService`）+ `jni/<abi>/libhev-socks5-tunnel.so`（放入 `app/libs/`，APK 仅打包 arm64-v8a）

## Build environment note

During this build, sandbox network access to github.com was restricted; the
amd64/arm64 ZIPs were fetched via the `gh-proxy.com` mirror. The mirrored content
is the same object as the official release; **before public release it is
recommended to re-download in an environment with direct GitHub access and
re-verify the hashes above** (`sha256sum <zip>`). The runtime fallback download
path always uses the official URLs and enforces the hashes above; any file with
a mismatched hash is never installed.

## Python dependencies (requirements.txt)

pyinstaller==6.10.0 · customtkinter==5.2.2 · darkdetect==0.8.0 · pytest>=8.0,<9 · pytest-mock>=3.12,<4

(After building, record the actually resolved versions from `pip freeze` in the release notes.)

---

## Windows lwIP 引擎（beta1.0.5 完全接管）

Windows 用户态协议栈完全由独立**原生 C/C++ 引擎 HevSocks5Tunnel（lwIP + Wintun）** 承接。
`engine.py` 的 `LwipEngine` 只负责生成配置、拉起 `hev-socks5-tunnel.exe`、互斥持有 Wintun、
隔离每用户配置、停机清理——**TCP/UDP/DNS 协议栈全在 C/lwIP 内以 PCB 统一处理，不写进 Python**，
旧版"多并发 DNS/UDP/TCP 各自开连接"的实现已废弃。

| Component | 版本/提交 | 源码 | SHA-256 | 许可证 |
|---|---|---|---|---|
| HevSocks5Tunnel (win64 exe) | 2.19.1 | 官方 Windows 构建 `releases/download/2.19.1/hev-socks5-tunnel-win64.zip` | `242557dc4ada5a45bcc02d98a558e86dd29025efdf7960b8e302bfc2ad1901c5` (exe, 316416 B) | MIT |
| msys-2.0.dll（官方 exe 的运行时依赖） | 随官方 zip | 同上 | `1410599ee2efcede0869abec8910398d688755952e59cf7983b15cac6de78201` (3368065 B) | GPL（MSYS2 运行时） |
| lwIP（Hev 子模块 third-part/lwip） | `e6d5fc6cdc7c2ab379ffffd13dfdc99fb826a86c` | 随 Hev 子模块（编译进 exe） | — | BSD-3-Clause |
| HevSocks5Core（子模块 src/core） | `9f1cf0bfca0c003dd0cf0fbdcf6912a2b46a8d05` | 随 Hev 子模块（编译进 exe） | — | MIT |
| Wintun | 0.14.1 | 官方 zip 内置（与 legacy 共用同一文件） | `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce` (427552 B) | 见 wintun.net |

- **采用官方 Windows 二进制（推荐路径，已采用）**：直接取 Hev 发布页的
  `hev-socks5-tunnel-win64.zip`，解出 `hev-socks5-tunnel.exe` / `wintun.dll` /
  `msys-2.0.dll` 三个文件放入 `laptop_proxy/engine/`。这是维护者自己的原生
  Windows 构建，规避了两类自编译问题：① MinGW 交叉编译缺 POSIX 兼容头
  （`netinet/in.h` 需完整 `mingw-w64-x86-64-dev` 包）；② Hev rbtree 的
  `unsigned long __parent_color` 在 Windows LLP64 下为 32 位，指针截断告警
  （`-Wpointer-to-int-cast`），自编译需 `-Wno-error` 且存在理论风险。
- **`msys-2.0.dll` 必须与 exe 同目录**（官方 exe 链接了 MSYS2 运行时）；
  `LaptopProxy.spec` 已把它列入 engine 打包清单。
- 自编译备选：`ci/build-lwip-wsl.sh`（WSL 交叉编译）或 `ci/build-lwip.ps1`
  （MSYS2）。注意 WSL 路线需安装完整 `mingw-w64` 元包（不能只装 gcc，否则缺
  `netinet/in.h`）。产物同样落 `laptop_proxy/engine/`，SHA-256 回填上表。
- **beta1.0.5 起 lwIP 为唯一内核**：`EngineManager` 仅含 `LwipEngine`，不再有 `LegacyEngine` /
  tun2socks 回退；`tun2socks.exe` 备份二进制已从安装包与 `bundled_bin/` 删除（任务书 104-G 的
  legacy 回退线已按 beta1.0.5 决策移除）。
- 首次/每次启动前由 `_cleanup_stale_tun_adapters` 清除**旧模式** LinkHelm/LinkHelm N/LaptopProxyVPN*
  适配器（含旧 tun2socks 遗留），但**显式保留**当前适配器 `LinkHelm-IwIP`（三层扫描均
  `-ne 'LinkHelm-IwIP'`）——实现"先清旧 → 建新 → 后续复用"：Wintun 按固定 GUID 重开同一
  适配器，不再出现 "LinkHelm-IwIP 2/3/4"，也不会出现"旧的清理不掉、新的也用不了"。
- 连接超时：`engine.py` 配置 `misc.connect-timeout: 25000`（手机 Wi-Fi Direct 拨号 +
  配额等待可达 15s+，上游默认 10s 太短会误判握手失败）。**注意**：`connect-timeout` /
  `tcp-read-write-timeout` / `udp-read-write-timeout` / `max-session-count` 只被 Hev 的
  `misc:` 段识别（`hev-config.c` 的 `hev_config_parse_misc`），放到 `socks5:` 会被
  **静默忽略**（未知键不报错，默认值继续生效）——beta1.0.5 已按官方 schema 归位。
- **不得配置 `misc.pid-file`**：`hev-main.c` 中 `if (pid_file) run_as_daemon(pid_file)`
  会使引擎 fork 成守护进程、父进程立即退出 0，从而破坏 main.py 的 Popen 生命周期
  （看门狗误判崩溃 → 重启循环）。引擎必须保持前台作为直接子进程。
- 排障：`LINKHELM_ENGINE` 环境变量仅作日志标记，不再切换内核。

---

## Android lwIP 引擎（beta1.0.5 完全接管）

手机端（Host/客户端 VPN 服务）同样弃用 tun2socks，改持**官方预编译 AAR**：
`hev-socks5-tunnel.aar`（tag 2.19.1）。AAR 内 `classes.jar` 提供 JNI 门面
`hev.htproxy.TProxyService`，`jni/arm64-v8a/libhev-socks5-tunnel.so` 为原生引擎；
`Myvpnservicetun2socks.kt` 的 `runLwip(fd)` 把 **VpnService 的 TUN fd** 直接交给
`TProxyService.TProxyStartService(config_path, fd)`——此时 Hev 走 `extern_tun_fd`
路径（跳过自建 Wintun 适配器），由 VpnService.Builder 负责 MTU/地址/路由/DNS，
lwIP 在其 PCB 栈内统一处理 TCP/UDP/DNS。

| Component | 版本 | 源码/资产 | SHA-256 | 许可证 |
|---|---|---|---|---|
| hev-socks5-tunnel.aar | 2.19.1 | `releases/download/2.19.1/hev-socks5-tunnel.aar` | `107958f7a07051386248ba8dba778b63d8dcefdfb2c3665b0ee9a8c93de5f6ca` (605512 B) | MIT |
| └ JNI 门面 `hev.htproxy.TProxyService` | — | AAR 内 `classes.jar` | 方法：`TProxyStartService(String,int)→boolean`、`TProxyStopService()→boolean`、`TProxyIsRunning()→boolean`、`TProxyGetStats()→long[]` | — |
| └ 原生库 `libhev-socks5-tunnel.so` | — | AAR 内 `jni/arm64-v8a/`（另有 armeabi-v7a/x86/x86_64，APK 经 `abiFilters` 仅打包 arm64-v8a） | — | MIT |

- **采用官方预编译 AAR（推荐路径，已采用）**：无需 NDK 交叉编译；AAR 与 Windows 端
  同一 tag（2.19.1），两端行为一致。
- `app/build.gradle.kts`：`implementation(files("libs/hev-socks5-tunnel.aar"))`
  （旧的 `libs/tun2socks-1.0.4.aar` 已删除）；`versionName = "beta1.0.5"`、
  `versionCode = 39`。
- **YAML 配置段位与 Windows 端相同**：`connect-timeout` 等超时必须落在 `misc:`
  （`socks5:` 下会被静默忽略）；**不得配置 `misc.pid-file`**（否则 Hev fork 成
  守护进程，JNI 调用会立即返回、VPN 服务生命周期失控）。
- 自编译备选：`ci/build-lwip-android.sh`（NDK 交叉编译，产物为同名 AAR）。
