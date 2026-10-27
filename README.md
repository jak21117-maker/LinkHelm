# LinkHelm

**Turn one Android phone's shared connection into a normal network for your other devices — over Wi-Fi Direct, locally, with nothing in the cloud.**

LinkHelm is a small, self-contained toolchain for a single, narrow job: let a Windows PC consume the internet connection that an Android phone is already sharing (Wi-Fi Direct / tethering), and then re-share that connection to other devices (a Wi-Fi hotspot or a plain Ethernet cable). Everything runs on your own hardware. There is **no server, no account, no telemetry, and no third-party relay** in the data path.

Two apps, one system:

| Component | What it is | Where it runs |
|---|---|---|
| **LinkHelm Host** (Android `.apk`) | Turns the phone into a local SOCKS5 endpoint on its Wi-Fi Direct / tethering network. | Android phone |
| **LinkHelm Desktop** (Windows `.exe`) | Connects to the phone's SOCKS5 endpoint, creates a local virtual adapter, and re-shares to other devices. | Windows PC |

Typical data path:

```
Android phone (already sharing / tethered)
      |  Wi-Fi Direct (SOCKS5, local only)
      v
Windows PC  -- LinkHelm Desktop -->  virtual adapter (10.0.0.1)
      |
      |--->  Wi-Fi hotspot   -->  laptops, tablets, consoles ...
      +--->  Ethernet cable  -->  a desktop / switch / router
```

---

**What's new in 1.1.0 (first stable release)** — the Windows desktop app is now the **native C++ edition**: same UI and behavior in a ~0.9 MB executable, instant startup, package shrunk from ~200 MB to ~40 MB. The store booster's subscription chain was rebuilt (system proxy → user proxy → live local-port probe → direct, with automatic sibling-domain failover), the latency test was fixed (UTF-8 percent-encoding of node names), share-link subscriptions (`vless://` `ss://` `vmess://` `trojan://` `hysteria2://`) are converted in-process, and the UI is now fully bilingual including node names. Measured throughput reached **70–80% of the host line speed** with **20–70 ms** typical latency. Both ends run **1.1.0**. The legacy Python edition remains under `laptop_proxy/` for study.

**Download** — get the signed APK and the Windows zip from the [Releases page](https://github.com/jak21117-maker/LinkHelm/releases/latest). Every release is built by GitHub Actions from its tag, ships a `SHA256SUMS.txt` checksum manifest, and pins all third-party binary sources in [`DEPENDENCY_LOCK.md`](DEPENDENCY_LOCK.md).

---

## Disclaimer, scope, and boundaries (read first)

Please read this section carefully. It defines what this software **is** and, just as importantly, what it **is not**.

1. **Built for personal learning and development.** LinkHelm is released for personal study, research, and development use. The code is licensed under the **MIT License** (see [LICENSE](LICENSE)) — the same terms as the upstream project — which also permits commercial use; if you intend to use it commercially, the author asks that you make contact first via **jak21117@gmail.com**. (c) 2026 LinkHelm Developers.

2. **It is not a general-purpose VPN or a network "unlock" tool.** LinkHelm does **not** tunnel arbitrary traffic through remote servers, does **not** hide your IP from the internet at large, and does **not** route around the policy of networks you do not control. The only thing it does is carry traffic **from your own Windows PC, through your own Android phone, out of the connection that phone already has.** Its "cross-proxy" capability is deliberately **locked to your own tethered phone** and cannot be pointed at an arbitrary upstream.

3. **You are responsible for how you use it.** You must comply with all applicable laws and the terms of service of the networks and carriers you use. Do not use LinkHelm on networks you are not authorized to use, and do not use it for any unlawful purpose. The developers accept **no liability** for misuse.

4. **No warranty.** This software is provided "as is", without warranty of any kind. Networking is inherently fragile; a driver update, a carrier policy change, or an OS update can break it. Use at your own risk.

5. **No hidden premium, no lock-in.** There are **no paid features** and nothing is gated behind a payment. The fixed initial credentials exist for reliability and are fully editable by you in Settings.

---

## What it actually does (introduction)

Modern phones can share their mobile/Wi-Fi connection over **Wi-Fi Direct**. LinkHelm makes that shared connection usable by a whole PC and, through the PC, by everything else:

- **LinkHelm Host (Android)** publishes a local **SOCKS5** proxy on the phone side, bound to the Wi-Fi Direct network, using a fixed, editable username/password (8-13 alphanumeric characters). It never opens anything to the public internet.
- **LinkHelm Desktop (Windows)** dials that SOCKS5 endpoint, brings up a local virtual network adapter (Wintun), installs the routes needed to reach the internet through it, and then re-shares that uplink to other devices - either as a **Windows Mobile Hotspot** or over a **wired Ethernet** adapter. Client devices need **no proxy configuration at all**; they just see a normal network.
- **Local file transfer** — move files between the phone and the Windows PC over the same link (Turbo mode), with no cloud involved. Single files up to **10 GB**, SHA-256 verified, and **resumable**: an interrupted transfer keeps its staged bytes and continues from the receiver's checkpoint when you re-send the same file.

The result: a phone's connection becomes a normal Wi-Fi/Ethernet network that any device can join.

---

## Requirements

### Windows (LinkHelm Desktop)
- Windows 10/11 (64-bit). Works without admin for normal use; it requests elevation only when it must configure sharing/routes.
- The bundled lwIP engine (`hev-socks5-tunnel` + `Wintun`) is included; nothing is downloaded at runtime.
- To share: either a Wi-Fi adapter that supports Mobile Hotspot, **or** a wired Ethernet adapter.

### Android (LinkHelm Host)
- Android 8.0 (API 26) or newer.
- Wi-Fi Direct capable hardware.

### A phone connection to share
- LinkHelm carries **the connection the phone already has** (its mobile data, or a Wi-Fi it is connected to). It does not create internet by itself.

---

## Quick start

### 1. Android - start the host
1. Install **LinkHelm Host** (`LinkHelm-Host-Android-*.apk`).
2. Open it, grant the requested permissions, and tap **Start host**.
3. Note the **SSID and password** shown (you can change them in Settings).
4. Keep the phone in the foreground. The phone is now a local SOCKS5 endpoint on its Wi-Fi Direct network.

### 2. Windows - connect
1. Run **LinkHelm Desktop** (`LinkHelm_Desktop.exe`).
2. **Step 1 - Inbound:** enter the **phone IP** (default `192.168.49.1`), the **SOCKS5 port** (default `8080`), and the SOCKS5 user/password (defaults match the app). Join the phone's Wi-Fi Direct network from Windows first. Tap **Connect**.
3. **Step 2 - Outbound:** choose **Wi-Fi Hotspot** or **Ethernet**, then tap **Start Sharing**.
4. On your other devices, join the hotspot SSID (or plug into the Ethernet) - no proxy setup needed.

See [`docs/MANUAL.md`](docs/MANUAL.md) for a concise walkthrough.

---

## Settings (both apps)

- **Language** - switch the interface between **English** and **Chinese**. On Windows the app restarts instantly to apply it everywhere (including the tray); on Android it applies live.
- **Start at logon** (Windows) - launch automatically after you sign in, minimized to the tray.
- **Auto-connect** (Windows) - after a silent logon start, connect automatically when the phone proxy appears.
- **SOCKS5 account** (both) - edit the username/password (8-13 alphanumeric). Changes apply immediately; if a connection is live the tunnel rebuilds on the fly.
- **Wi-Fi SSID / password** (Android) - the hotspot your PC joins.
- **ICS diagnostics** (Windows) - a read-only check of the sharing service and the route to the phone. It changes nothing.

---

## Security notes

- **Local only.** The SOCKS5 endpoint is bound to the Wi-Fi Direct link between your phone and your PC. It is not reachable from the internet.
- **Fixed but editable credentials.** The initial username/password are identical on both ends and never randomized. Change them in Settings if you share the network with others.
- **Verified binaries.** The bundled `hev-socks5-tunnel`/`Wintun` engine files are SHA-256 checked against pinned values before use.
- **ICS diagnostics are read-only.** The Windows diagnostics never modify sharing configuration.

---

## Measured performance (one test round)

Measured on the developer's own setup: the same phone, the same laptop client joining the PC's hotspot, the same game server/region and time window, one client active. This is a LinkHelm **scenario test** — numbers vary with radio conditions, carrier and device, and are not a guarantee for every device.

### Version-over-version progress

| Metric (single client) | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| Throughput vs host line speed | ~30% | ~50% | ~60% | **70–80%** |
| Typical latency | 100–200 ms | ~100 ms | 30–80 ms (occasionally 100) | **20–70 ms** |

The jump from beta1.0.0 to beta1.0.56 came from replacing the legacy tun2socks core with the native lwIP engine; 1.1.0 adds the tuned C++ edition on top.

### Legacy vs lwIP engine (beta1.0.56 era)

The throughput figures line up with the version table above: the legacy engine spans beta1.0.0–1.0.3 (30–50%), the lwIP engine spans beta1.0.56–1.1.0 (60–70%+).

| Metric (single client) | Legacy engine (tun2socks, beta1.0.0–1.0.3) | lwIP engine (beta1.0.56–1.1.0) |
|---|---|---|
| In-game round-trip, typical | 60–70 ms | **20–30 ms** |
| In-game round-trip, worst observed | often >100 ms | occasional 50–60 ms |
| p95 round-trip | ~110 ms | ~45 ms |
| Jitter (1-minute window) | 15–30 ms | 3–8 ms |
| Download through the hotspot | 30–50% of the phone's direct speed | 60–70% of the phone's direct speed |
| Engine CPU on the PC (1-client mixed load) | 8–12% of one core | 3–5% of one core |

Reproduce it: on the same link in the same hour, record in-game ping, a long `ping -t` from a hotspot client, and a large-file download — three rounds per engine. Session capacity is a design target: per-client quotas support hundreds of concurrent sessions on one device, with many devices sharing at once.

## File transfer & store boost evolution

| Capability | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| File transfer | manual, no resume | real file names kept | resumable, SHA-256 verified | **resumable + real names + 10 GB + live sync status** |
| Store boost | — | — | mihomo kernel, Clash YAML only | **any subscription format, auto proxy detection, latency test, bilingual UI** |

---

## FAQ

**Does this hide my IP or act like a VPN server?**
No. It forwards your PC's traffic out through your own phone's existing connection. It is not a remote VPN.

**Can I point it at some other SOCKS5 server?**
No. The upstream is deliberately restricted to your own tethered phone.

**Do client devices need proxy settings?**
No. They join a normal hotspot/Ethernet network.

**Windows says "already running".**
Only one instance may run at a time (two would fight over routes and sharing). Quit the first one from the tray via **Quit & Clean Up Network**.

**The hotspot never appears / sharing fails.**
Run **ICS Diagnostics** in Windows Settings (read-only) and check the route to the phone. Make sure the PC is joined to the phone's Wi-Fi Direct network first.

**Language did not switch.**
Windows: it restarts automatically; if it did not, reopen Settings. Android: it applies immediately.

---

## Building from source

Both apps build from this repository. The Windows EXE is the native C++ edition (`cpp/`, MinGW-w64 + CMake); the Android APK with Gradle. The legacy Python edition (`laptop_proxy/`) is kept for study and comparison.

```bash
# Android
cd app && ../gradlew assembleDebug        # or assembleRelease

# Windows (native C++ edition, MinGW-w64 toolchain)
cmake -S cpp -B cpp/build -G "MinGW Makefiles" && cmake --build cpp/build --parallel
```

Pinned dependency sources and their SHA-256 values are documented in the source tree. Only the latest version ships in the release archives.

---

## License

Licensed under the MIT License (see [LICENSE](LICENSE)) — the same terms as the upstream project. The project is **aimed at personal learning and development**; that positioning is the author's focus, not an extra restriction on the MIT grant, which itself permits commercial use (the author asks for prior contact via **jak21117@gmail.com**). Third-party components keep their own licenses — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Provided "as is", without warranty. See the disclaimer above.

---

## Support my research

Development and maintenance of LinkHelm are done in the author's own time. If it saved you a headache, a completely voluntary tip is welcome - there are **no premium features** and nothing is gated behind a donation.

**Support my research, donations are voluntary. No premium features.**

<!-- Donation QR code: sponsor_qr.png ships next to this README -->
<img src="sponsor_qr.png" alt="Donate to support my research" width="240"/>

---

## Acknowledgements and credits

LinkHelm is a derivative work based on the open-source project **Hotspot-Bypass-VPN-Unlimited-Hotspot** by nestchao.

- Original project: <https://github.com/nestchao/Hotspot-Bypass-VPN-Unlimited-Hotspot>

LinkHelm keeps the original idea — sharing a phone's connection locally over Wi-Fi Direct through a SOCKS5 proxy — but has been substantially rewritten and extended. Compared with the original, the main improvements are:

- **Unified lwIP engine on both ends** — the legacy tun2socks core is fully replaced by the native lwIP stack engine (HevSocks5Tunnel 2.19.1): one kernel handles TCP/UDP/DNS on both Android and Windows, with pinned, SHA-256-verified binaries and no runtime download or fallback paths.
- **Stronger TCP and UDP connection stability** — more robust connection lifecycle, idle keep-alive, and retransmission handling, especially under gaming / UDP-heavy workloads.
- **Multiple DNS fallbacks** — several DNS servers are tried in sequence so name resolution survives when one is unreachable.
- **SOCKS5 credential authentication (RFC 1929)** — the host enforces username/password auth, so there is no open, unauthenticated port.
- **Greatly improved concurrency** — higher worker limits and per-client quota management let many devices share at once without starving later connections.
- **Dual-channel (primary + standby lane)** — an extra lane keeps connectivity alive and degrades gracefully under congestion.
- **Most of the code has been rewritten** — the proxy engine, the Windows client, and the Android host were reworked substantially beyond the original.

Licensed under the same MIT terms as the original (see [LICENSE](LICENSE)).

If you believe any part of this project infringes your rights, or if you find a bug, please contact **jak21117@gmail.com** — infringing content can be removed on request, and bug reports are also welcome at the same address.
