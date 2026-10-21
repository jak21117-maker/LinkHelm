# LinkHelm — Quick Manual / 简要操作手册

This is the short version. Full details are in the [README](../README.md).

---

## English

### A. Android phone (the host — the device that HAS the connection)
1. Install **LinkHelm Host** and open it.
2. Grant the permissions it asks for (location / nearby devices / notifications).
3. On the **Host** tab, tap **Start host**.
4. Write down the **SSID**, **password**, **IP** and **port** shown on the info card.
5. Leave the phone on this screen. It is now sharing its connection locally.

### B. Windows PC (the client — re-shares to other devices)
1. On Windows, join the phone's Wi-Fi Direct network (the SSID from step A.4).
2. Run **LinkHelm Desktop**.
3. **Step 1 — Inbound:** type the phone **IP** (`192.168.49.1`) and **SOCKS5 port** (`8080`). Username/password default to the built-in pair (edit in Settings if you changed them). Tap **Connect**. Wait for "Connected".
4. **Step 2 — Outbound:** pick **Wi-Fi Hotspot** (or **Ethernet** if you plugged a cable) and tap **Start Sharing**.
5. Connect your other devices to that hotspot / cable. **No proxy settings needed.**

### C. Settings you may want
- **Language**: English / 中文 (Windows restarts to apply; Android applies live).
- **Start at logon / Auto-connect** (Windows): start and connect automatically.
- **SOCKS5 account** (both): change the 8–13 character username/password; applies immediately.
- **ICS Diagnostics** (Windows): read-only health check if sharing misbehaves.

### Stop / exit
- Windows: close the window → choose *minimize to tray* or *Quit & Clean Up Network* from the tray icon.
- Android: tap **Stop host**.

---

## 中文

### A. 安卓手机（主机——已有网络的一方）
1. 安装并打开 **LinkHelm Host**。
2. 授予所需权限（位置 / 附近设备 / 通知）。
3. 在**共享（主机）**栏点击**开启共享**。
4. 记下信息卡上的 **SSID、密码、IP、端口**。
5. 让手机停在该界面，即开始本地共享。

### B. Windows 电脑（客户端——再共享给其他设备）
1. 在 Windows 上加入手机的 Wi-Fi Direct 网络（A.4 的 SSID）。
2. 运行 **LinkHelm Desktop**。
3. **第 1 步 入站：** 填写手机 **IP**（`192.168.49.1`）与 **SOCKS5 端口**（`8080`）；账号密码默认为内置值（改过则在设置里填新的）。点击**连接**，等待“已连接”。
4. **第 2 步 出站：** 选择 **Wi-Fi 热点**（或插了网线选**以太网**），点击**开始共享**。
5. 其他设备加入该热点 / 网线即可，**无需任何代理设置**。

### C. 可能用到的设置
- **语言**：English / 中文（Windows 会重启生效；安卓即时生效）。
- **登录后启动 / 自动连接**（Windows）：开机自动启动并连接。
- **SOCKS5 账户**（两端）：修改 8–13 位账号密码，立即生效。
- **ICS 诊断**（Windows）：共享异常时的只读体检。

### 停止 / 退出
- Windows：关闭窗口 → 选择*最小化到托盘*，或在托盘图标选择*退出并清理网络*。
- 安卓：点击**停止共享**。

---

> Reminder / 提示: built for personal learning & development use; licensed under MIT — for commercial use the author asks for prior contact (jak21117@gmail.com); the upstream is locked to your own tethered phone. 面向个人学习开发使用；按 MIT 许可证授权，如需商用请先联系作者 (jak21117@gmail.com)；上游仅限你本人共享的手机。
