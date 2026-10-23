# THIRD_PARTY_NOTICES

This repository is a modified version of **Hotspot-Bypass-VPN-Unlimited-Hotspot**
(nestchao, MIT). The original copyright and the full MIT text are in the root
`LICENSE`; the changes made in this edition are tracked separately.

## Third-party binaries shipped with the package

| Component | Version | License | Source |
|---|---|---|---|
| tun2socks (xjasonlyu) Windows binary | v2.7.0 — **REMOVED in beta1.0.5** | (was GPL-3.0) | github.com/xjasonlyu/tun2socks/releases |
| Wintun prebuilt DLL | 0.14.1 | dedicated license text shipped with the official prebuilt package (see `wintun/LICENSE.txt`) | www.wintun.net/builds |
| Mihomo kernel Windows binary (`laptop_proxy/mihomo/mihomo.exe`) | v1.19.33 (pinned) | **GPL-3.0** | github.com/MetaCubeX/mihomo/releases/tag/v1.19.33 |

**GPL-3.0 obligations (Mihomo)**: the binary is shipped **UNMODIFIED** and runs
as a **separate process** (it is never linked or compiled into LinkHelm), so
the GPL does not extend to LinkHelm (MIT). When distributing the binary you
must also provide the complete corresponding source of that version (or a
written offer, valid for at least 3 years) together with the full GPL-3.0
text. Corresponding source: https://github.com/MetaCubeX/mihomo
(tag `v1.19.33`). LinkHelm bundles and fetches **no proxy nodes**; users
import their own subscription or config locally.

Every bundled binary is SHA-256 verified against pinned values before use.

## Android dependencies

| Component | Version | License | Status |
|---|---|---|---|
| app/libs/tun2socks-1.0.4.aar | 1.0.4 | **provenance to be verified** (no bundled license/source evidence; contains multi-ABI libgojni.so) | Before any public release, complete the provenance, build process and license record; until then the APK is for local functional verification only |
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
- GPL-3.0 obligations for tun2socks are fulfilled as described above.
- Until the Android AAR provenance is verified, the APK is **not** published
  publicly (internal/local testing only).
