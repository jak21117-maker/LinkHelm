# CHANGES beta1.0.57

Windows 端新增 **商店加速器**（Steam / Epic）。APK 不变（手机端维持 beta1.0.56 / versionCode 45）。

## 新增：商店加速（01.5 面板）

- 界面在「01 入站连接」与「02 出站分发」之间新增 **01.5 商店加速** 卡片：
  导入订阅 / config.yaml → 一键启动/停止，可选"设置系统代理"。
- 内核：官方 **Mihomo v1.19.33**（GPL-3.0），以**未修改的独立子进程**运行，
  GPL 不传染 LinkHelm（MIT）；来源与哈希见 `DEPENDENCY_LOCK.md`，
  GPL 源码链接见 `THIRD_PARTY_NOTICES.md`。
- **不创建新网卡**：仅监听 `127.0.0.1:7890`（mixed 端口）+ 可选 WinINET
  系统代理；出站沿用系统现有路由（连接手机隧道时走隧道，否则走物理网卡）。
- **分流规则**：仅 Steam（steampowered / steamcommunity / steamstatic /
  steam-chat / steamserver）与 Epic（epicgames / epicgames.dev /
  unrealengine）域名走代理节点，**其余全部 DIRECT**——游戏与日常流量零影响，
  且无需下载 GeoIP 数据库。
- **不内置、不抓取、不分发任何节点**：节点由用户自行导入（订阅 URL 或本地
  config.yaml），配置仅保存在本机 `%APPDATA%\LaptopProxy\accel.json`，
  不随包分发。导入本地 config.yaml 时按用户自己的规则运行。
- 系统代理开关会先记录原状态，停止加速 / 退出程序（含托盘退出）时自动还原，
  不会残留失效代理导致"上不了网"。
- mihomo.exe 随包分发前按固化 SHA-256 校验（`store_accel.find_mihomo_exe`），
  哈希不符拒绝运行。

## 与其他加速器的关系

- 小黑盒 / UU 等加速器自带虚拟网卡驱动并接管全局路由，与任何代理叠加
  会形成"双 TUN 抢路由"，不建议同时开启；本面板的角色是**替代**它们完成
  商店域名分流，而非与其叠加。

## 限制说明

- 系统代理方式对 Steam 客户端内嵌浏览器（商店/社区页面）与 Epic 启动器
  生效；Steam **游戏下载**走其自身 CDN 直连，不在加速范围（这正是
  "仅商店域名"规则的设计意图）。
- mihomo 首次拉取订阅需能访问订阅地址；如订阅托管在 GitHub raw，
  可自行替换为可访问的镜像前缀。

## 验证

- pytest 108 passed（新增 `tests/test_store_accel.py` 6 项：配置生成无内置
  节点、商店规则与 DIRECT 兜底、随包内核哈希校验、空订阅拒绝、非法输入拒绝、
  状态机初始态）。
- `py_compile` 通过；spec 新增 `mihomo/mihomo.exe` 可选打包段（与 engine/ 同模式）。

## 升级说明

- Windows：直接覆盖安装 beta1.0.57。
- Android：**无改动**，继续使用 beta1.0.56 APK（versionCode 45）。
