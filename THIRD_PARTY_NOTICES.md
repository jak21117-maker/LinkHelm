# THIRD_PARTY_NOTICES

This repository is a modified version of **Hotspot-Bypass-VPN-Unlimited-Hotspot**
(nestchao, MIT). The original copyright and the full MIT text are in the root
`LICENSE`; the changes made in this edition are tracked separately.

## Third-party binaries shipped with the package

| Component | Version | License | Source |
|---|---|---|---|
| tun2socks (xjasonlyu) Windows binary | v2.7.0 — **REMOVED in beta1.0.5** | (was GPL-3.0) | github.com/xjasonlyu/tun2socks/releases |
| Wintun prebuilt DLL | 0.14.1 | dedicated license text shipped with the official prebuilt package (see `wintun/LICENSE.txt`) | www.wintun.net/builds |
| Mihomo kernel (`mihomo/mihomo.exe`) | MetaCubeX official **v1.19.33**, SHA-256 `1cde41cf…` (61,247,488 B) | **GPL-3.0** | github.com/MetaCubeX/mihomo/releases (official `mihomo-windows-amd64-v1.19.33.zip`) |
| Mihomo kernel (`mihomo/verge-mihomo.exe`) | Clash Verge build of Mihomo **v1.19.25**, SHA-256 `c897eeb2…` (46,177,792 B) | **GPL-3.0** | shipped inside the official Clash Verge client (clash-verge-rev/clash-verge); upstream: github.com/MetaCubeX/mihomo |

**GPL-3.0 obligations (both Mihomo kernels)**: both binaries are shipped **UNMODIFIED** and run as **separate processes** (never linked or compiled into LinkHelm), so the GPL does not extend to LinkHelm (MIT). The store accelerator tries both kernels and uses whichever parses the given subscription. When distributing the binaries you must also provide the complete corresponding source of those versions (or a written offer, valid for at least 3 years) together with the full GPL-3.0 text. Corresponding source: https://github.com/MetaCubeX/mihomo (tags v1.19.33 and v1.19.25 respectively). LinkHelm bundles and fetches **no proxy nodes**; users import their own subscription or config locally.

Every bundled binary is SHA-256 verified against pinned values before use.

## Android dependencies

| Component | Version | License | Status |
|---|---|---|---|
| app/libs/hev-socks5-tunnel.aar | 2.19.1 | **MIT** | official prebuilt AAR from github.com/heiher/hev-socks5-tunnel (SHA-256 `107958f7…`, see DEPENDENCY_LOCK.md); the legacy tun2socks-1.0.4.aar was removed in beta1.0.5 |
| AndroidX / Kotlin / Material etc. | see gradle/libs.versions.toml | respective licenses (Apache-2.0 etc.) | full manifest generated at release time |

## Python dependencies (Windows client)

| Component | License |
|---|---|
| PyInstaller | GPL-2.0 or later (with a bootloader exception; the PyInstaller bootloader has its own license notice) |
| customtkinter | MIT |
| darkdetect | MIT (BSD-3-Clause compatible, see its repository) |
| pystray | MIT |
| pillow | MIT-CMU |
| pytest / pytest-mock | MIT |

> Note: PyInstaller packaging does not make your own program GPL-infected (its
> FAQ states this explicitly), but please ship this file together with the
> distribution, plus the PyInstaller bootloader license notice.

## License conclusion

- The main project continues to be distributed under **MIT**, retaining the
  original copyright notice and the full LICENSE text.
- GPL-3.0 obligations for the two Mihomo kernel binaries are fulfilled as
  described above (unmodified, separate process, corresponding source
  pointers for both versions).
- The Android engine (hev-socks5-tunnel 2.19.1, MIT) and the Windows lwIP
  engine (same project, MIT) are permissively licensed; no provenance
  blockers remain.
