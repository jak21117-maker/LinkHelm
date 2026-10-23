# LinkHelm beta1.0.56

Windows 与 Android 双端统一版本号 beta1.0.56（Android versionCode 45）。
本版完成双端数据面架构统一，连接体验、会话容量、速率与稳定性全面升级。
研发测试构建。

## 架构更新：双端统一 lwIP 内核（HevSocks5Tunnel 2.19.1）

- Windows 与 Android 的数据面统一为同一原生 lwIP 内核：TCP / UDP / DNS
  全部由 lwIP 的 PCB 栈在单一内核内处理，消除旧架构多链路并存的额外开销。
- 引擎三件套随包分发（`hev-socks5-tunnel.exe` + `wintun.dll` +
  `msys-2.0.dll`），启动前按固化 SHA-256 校验，运行时零下载、零回退，
  行为完全可复现。
- Windows 侧保持轻量 Python GUI 与编排（PyInstaller 打包），数据面全部
  下沉到原生引擎。

## 体验提升

- 虚拟网卡 **LinkHelm-IwIP** 采用固定 GUID：Wintun 按 GUID 直接重开同一
  适配器，再次连接秒级就绪，不再累积 "LinkHelm-IwIP 2/3/4" 冗余网卡。
- 单实例互斥 + 托盘"退出并清理网络"；开机自启、静默启动后自动连接，
  手机代理一出现即完成拨号。
- 中英文双语界面即时切换；SOCKS5 账号修改即时生效，隧道在线重建。

## 会话数量与并发

- 安卓侧把原先分散的三路握手（TCP 预检 / SOCKS5 保活 / UDP 自测）收敛为
  **单次统一握手**，深度探测改为任一候选成功即过，活动连接与会话数
  大幅下降，多设备并发更从容。
- 按客户端的会话配额与队列统一管理，多设备同时共享互不挤占。

## 速率与延时

- lwIP 单栈处理显著减少内核态 / 用户态往返，转发路径更短，**延时更低**。
- 链路参数按手机 Wi-Fi Direct 场景整体调优（连接窗口、保活周期、
  重传节奏），弱信号下吞吐更平稳。

## 稳定性

- 引擎以前台直接子进程运行，配合进程守护与自动重建，长时运行更稳。
- 过期隧道路由自动清扫、NCSI 探测状态随断开自动还原，网络环境始终干净。
- 手机端看门狗兜底，共享意外中断后自动拉起。

## 打包

- Windows zip 只含应用目录（`LinkHelm_Desktop/`）；Android APK 仅打包
  arm64-v8a，体积更小。
- 引擎二进制来源与 SHA-256 见 `DEPENDENCY_LOCK.md`（官方 GitHub release /
  wintun.net，哈希固化）。

## 验证

- Windows：pytest 单测 102 passed（引擎启动、隧道生命周期、UDP/DNS 池、
  单实例、二进制完整性等）。
- Android：单元 + 集成测试通过（客户端名册、代理服务器、UDP 探针）。

---

Binaries: signed Android APK (versionCode 45) + Windows win64 zip, both built
and published by GitHub Actions from this tag.
