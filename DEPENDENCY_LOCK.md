# DEPENDENCY_LOCK — External Binary Lock Manifest

Builder verification date: 2026-10-15. All hashes are SHA-256 computed on the **complete ZIP files** downloaded from official distribution points.

| Component | Version | Official source URL | SHA-256 (ZIP) |
|---|---|---|---|
| tun2socks (Windows amd64) | v2.7.0 | https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-amd64.zip | `c5d46e9452f6c9cc7c15ab9158d6d6a0169ceecd6bca019ce476b49337d2be43` |
| tun2socks (Windows arm64) | v2.7.0 | https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-arm64.zip | `74497771068da13f42921adfc540f2abb9ac822404582c8cbe34d30e8c0ea1f5` |
| Wintun | 0.14.1 | https://www.wintun.net/builds/wintun-0.14.1.zip | `07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51` |

## Bundled binary notes

- The bundled `tun2socks.exe` is a **local rebuild of the official v2.7.0 source with a
  single-constant patch**: `tcpConnectTimeout` in `tunnel/tunnel.go` changed from
  **5s to 20s**. The upstream 5-second cap aborts CONNECT when the phone side
  legitimately needs longer (15s dial timeout + 1.5s quota wait), causing client
  retry storms; 20s restores the patience of earlier builds. Everything else is
  kept identical to the official v2.7.0 build (parallel capability unchanged).
- Build: Go 1.27.2, `go build -trimpath -ldflags "-s -w -X ...version.Version=2.7.0 -X ...version.GitCommit=8dda19e-linkhelm"`,
  module proxy goproxy.cn; `-v` reports `tun2socks-2.7.0 / windows/amd64, go1.27.2, 8dda19e-linkhelm`.
- Bundled exe SHA-256 (unpacked): `9c27b029f751fccda8f8c948f21f0a2c7676752ae207215d4bc5b461961be70b`.
  The official unpatched v2.7.0 exe (`076b3c3d6a372bae3f49f2b415a4105f70c30a3ed3caaed7979390e649892559`)
  is kept in the source tree at `laptop_proxy/bundled_bin/tun2socks_v2.7.0_official.exe`
  for reference only and is not shipped.
- Runtime verification gate: `tun2socks.exe --version` must report `2.7.0` +
  `8dda19e-linkhelm`; otherwise the binary is rejected and reinstalled from the
  bundled copy. Any legacy `tun2socks_*.exe` leftovers in the runtime cache
  (`%APPDATA%\LaptopProxy\bin`) are cleaned at startup. The recovery source is
  unique: only the bundled locked build (`9c27b029…`).
- If the bundled exe is missing and the fallback download triggers, it fetches the
  **official** binary (5s cap), whose hash will not match and will be rejected —
  always ship the bundled exe; do not rely on the fallback path.

## Exact ZIP members used

- amd64: `tun2socks-windows-amd64.exe` (renamed to `tun2socks.exe` when bundled)
- arm64: `tun2socks-windows-arm64.exe`
- wintun: `wintun/bin/amd64/wintun.dll` (`wintun/LICENSE.txt` is also kept for license compliance)

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

## Windows lwIP 引擎（beta1.0.4 新增，任务书 104-B）

Windows 用户态协议栈由独立**原生 C/C++ 引擎**承接：保留 tun2socks 作为 legacy
回退，新增基于 **HevSocks5Tunnel（lwIP + Wintun）** 的对照内核。`engine.py` 的
`LwipEngine` 只负责生成配置、拉起 `hev-socks5-tunnel.exe`、互斥持有 Wintun、隔离
每用户配置、停机清理——**TCP/UDP/DNS 协议栈全在 C 里，不写进 Python**。

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
- **默认内核已切换为 lwIP（原生 C/C++）**：`EngineManager` 默认 `mode="lwip"`，只要
  `laptop_proxy/engine/hev-socks5-tunnel.exe` 存在即启用；二进制缺失时**自动回退**到
  备份 tun2socks（单例互斥持有 Wintun，两内核绝不同时在线，避免抢包"打架"）（任务书 104-G）。
- 排障时可设环境变量 `LINKHELM_ENGINE=legacy` 强制走备份 tun2socks。
- 旧 tun2socks v2.7.0 + 20s CONNECT 补丁作为**备份二进制保留**在安装包中；lwIP 不可用时
  无缝回退，放入 `hev-socks5-tunnel.exe` 后下次启动自动切回 lwIP，无需改动任何设置。
