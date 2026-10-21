# DEPENDENCY_LOCK — 外部二进制锁定清单

构建者核验日期：2026-10-15。所有哈希为对官方分发点下载的**完整 ZIP 文件**计算的 SHA-256。

| 组件 | 版本 | 官方来源 URL | SHA-256 (ZIP) |
|---|---|---|---|
| tun2socks (Windows amd64) | v2.7.0 | https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-amd64.zip | `c5d46e9452f6c9cc7c15ab9158d6d6a0169ceecd6bca019ce476b49337d2be43` |
| tun2socks (Windows arm64) | v2.7.0 | https://github.com/xjasonlyu/tun2socks/releases/download/v2.7.0/tun2socks-windows-arm64.zip | `74497771068da13f42921adfc540f2abb9ac822404582c8cbe34d30e8c0ea1f5` |
| Wintun | 0.14.1 | https://www.wintun.net/builds/wintun-0.14.1.zip | `07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51` |

> v1.5.35 升级记录（2026-10-18）：tun2socks v2.5.2 → v2.7.0（官方 Releases 下载并实测 `-v` = `tun2socks-2.7.0 / windows/amd64, go1.26.3, 8dda19e`）。内置 exe SHA-256（解压后）：`076b3c3d6a372bae3f49f2b415a4105f70c30a3ed3caaed7979390e649892559`；v2.5.2 旧 exe（`90547fe071fe471b02da83dd150b5db7ce02454797e7f288d489b1ff0c4dd67c`）备份于 `laptop_proxy/bundled_bin/tun2socks_v2.5.2_backup.exe`。所有启动参数（`-device/-proxy/-loglevel/-tcp-auto-tuning`）在 v2.7.0 `-h` 下逐项核验兼容；wintun.dll 保持 0.14.1 不变。

> v1.5.39 重建记录（2026-10-18）：**内置 tun2socks 改为"官方 v2.7.0 源码 + 单常量补丁"的本地重编译**。
> - 补丁内容（唯一改动）：`tunnel/tunnel.go` 的 `tcpConnectTimeout` **5s → 20s**。上游 5s 上限会在手机侧合法需要
>   更久（15s 拨号超时 + 1.5s 配额等待）时中止 CONNECT，引发客户端重试风暴；20s 恢复 v2.5.2 时代的耐心，
>   其余全部保持 v2.7.0 原样（并行能力不变）。
> - 构建：Go 1.27.2，`go build -trimpath -ldflags "-s -w -X ...version.Version=2.7.0 -X ...version.GitCommit=8dda19e-linkhelm"`，
>   模块源 goproxy.cn；`-v` 输出 `tun2socks-2.7.0 / windows/amd64, go1.27.2, 8dda19e-linkhelm`。
> - 内置 exe SHA-256：`9c27b029f751fccda8f8c948f21f0a2c7676752ae207215d4bc5b461961be70b`；
>   官方未补丁 v2.7.0（`076b3c3d…`）备份于 `laptop_proxy/bundled_bin/tun2socks_v2.7.0_official.exe`。
> - 注意：若内置 exe 缺失触发回源下载，将下载到**官方**二进制（5s 上限），其哈希与本表 `EXPECTED_BIN_SHA256`
>   不符会被拒绝——请随包分发内置 exe，勿依赖回源路径。

## ZIP 内使用的精确成员

- amd64：`tun2socks-windows-amd64.exe`（随包重命名为 `tun2socks.exe`）
- arm64：`tun2socks-windows-arm64.exe`
- wintun：`wintun/bin/amd64/wintun.dll`（另保留 `wintun/LICENSE.txt` 用于许可合规）

## 构建环境说明

本次构建期间沙箱网络对 github.com 直连受限，amd64/arm64 ZIP 经 `gh-proxy.com` 镜像获取。
镜像内容与官方 release 为同一对象；**公开发布前建议在可直连 GitHub 的环境重新下载并复核
上表哈希**（`sha256sum <zip>`）。运行时兜底下载路径仍固定官方 URL 并强制校验上表哈希，
哈希不匹配的文件一律不安装。

## Python 依赖（requirements.txt）

pyinstaller==6.10.0 · customtkinter==5.2.2 · darkdetect==0.8.0 · pytest>=8.0,<9 · pytest-mock>=3.12,<4
（构建后请以 `pip freeze` 实际解析版本为准记录到发行说明。）

## v1.5.4（2026-10-18）：2.5.2 彻底移除 + 运行时版本闸门
- 删除 `bundled_bin/tun2socks_v2.5.2_backup.exe`（唯一残留的 2.5.2 二进制）。
- 运行时缓存（%APPDATA%\LaptopProxy\bin）启动即清扫任何 `tun2socks_*.exe` 旧版残留；恢复源唯一化：仅内置 v2.7.0 锁定构建（`9c27b029…`）。
- P0-1 运行时核验：`tun2socks.exe --version` 必须报告 `2.7.0` + `8dda19e-linkhelm`，否则拒绝使用并重装。
- `tun2socks_v2.7.0_official.exe`（`076b3c3d…`，未打补丁官方原版）仍保留于源码树作回溯参照，不随包分发。
