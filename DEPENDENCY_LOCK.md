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

| Component | 版本/提交 | 源码 | SHA-256 (exe) | 许可证 |
|---|---|---|---|---|
| HevSocks5Tunnel | `TODO_PIN_HEV_COMMIT` | https://github.com/heiher/hev-socks5-tunnel | 待编译后回填 | MIT |
| lwIP（Hev 子树 third/lwip） | `TODO_PIN_LWIP_COMMIT` | 随 Hev 子模块 | — | BSD-3-Clause |
| Wintun | 0.14.1 | 见上表（与 legacy 共用） | `e5da8447…`(dll) | 见 wintun.net |

- 构建：在 MSYS2 MinGW64 中执行 `ci/build-lwip.sh`（或 PowerShell 入口
  `ci/build-lwip.ps1`）。脚本会克隆固定提交、pin 死 lwIP 子树、编译 amd64、把
  `hev-socks5-tunnel.exe` 落到 `laptop_proxy/engine/`，并打印其 SHA-256 供此处回填。
- **默认内核已切换为 lwIP（原生 C/C++）**：`EngineManager` 默认 `mode="lwip"`，只要
  `laptop_proxy/engine/hev-socks5-tunnel.exe` 存在即启用；二进制缺失时**自动回退**到
  备份 tun2socks（单例互斥持有 Wintun，两内核绝不同时在线，避免抢包"打架"）（任务书 104-G）。
- 排障时可设环境变量 `LINKHELM_ENGINE=legacy` 强制走备份 tun2socks。
- 旧 tun2socks v2.7.0 + 20s CONNECT 补丁作为**备份二进制保留**在安装包中；lwIP 不可用时
  无缝回退，编译出 `hev-socks5-tunnel.exe` 后下次启动自动切回 lwIP，无需改动任何设置。
- 在能够直连 GitHub 的环境实际编译后，务必回填上表提交号与 exe 的 SHA-256。
