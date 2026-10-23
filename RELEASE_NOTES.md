# LinkHelm beta1.0.56

Windows and Android now share one version number: **beta1.0.56** (Android
versionCode 45). This release unifies the data-plane architecture on both
ends and upgrades connection experience, session capacity, throughput and
stability across the board. Development/test build.

## Architecture: unified lwIP engine on both ends (HevSocks5Tunnel 2.19.1)

- The data plane on Windows and Android now runs the **same native lwIP
  kernel**: TCP / UDP / DNS are all handled by lwIP's PCB stack in a single
  kernel, removing the overhead of the old multi-link design.
- The engine trio ships inside the package (`hev-socks5-tunnel.exe` +
  `wintun.dll` + `msys-2.0.dll`); SHA-256 is verified at startup against
  pinned values. No downloads and no fallback paths at runtime — behavior
  is fully reproducible.
- Windows keeps a lightweight Python GUI/orchestration layer (PyInstaller
  packaged); the data plane lives entirely in the native engine.

## Experience

- The **LinkHelm-IwIP** virtual adapter uses a fixed GUID: Wintun reopens
  the same adapter directly, so reconnects are ready in seconds and the
  old "LinkHelm-IwIP 2/3/4" adapter accumulation is gone.
- Single-instance guard plus a tray "Quit & Clean Up Network"; start at
  logon with silent auto-connect — dialing completes as soon as the phone
  proxy appears.
- Instant English/Chinese UI switching; SOCKS5 credential changes apply
  live and the tunnel rebuilds on the fly.

## Sessions & concurrency

- On Android, the previously separate three-way probing (TCP pre-check /
  SOCKS5 keep-alive / UDP self-test) is consolidated into a **single
  unified handshake** — active connections and session count drop sharply,
  so many devices can share comfortably at once.
- Per-client session quotas and unified queue management keep concurrent
  devices from starving each other.

## Speed & latency

- Single-stack lwIP processing cuts kernel/user-mode round-trips and
  shortens the forwarding path for **lower latency**.
- Link parameters are tuned for phone Wi-Fi Direct (connection window,
  keep-alive cadence, retransmission pacing) to keep throughput smooth
  under weak signal.

## Stability

- The engine runs as a foreground child process with a supervisor and
  automatic rebuild, for steady long-running sessions.
- Stale tunnel routes are swept automatically and NCSI probing state is
  restored on disconnect, keeping the network environment clean.
- A phone-side watchdog re-arms sharing automatically after an
  interruption.

## Packaging

- The Windows zip contains only the app folder (`LinkHelm_Desktop/`); the
  Android APK ships arm64-v8a only, for a smaller download.
- Engine binary sources and SHA-256 values are pinned in
  `DEPENDENCY_LOCK.md`.

## Verification

- Windows: 102 pytest tests passed (engine launch, tunnel lifecycle,
  UDP/DNS pool, single instance, binary integrity, and more).
- Android: unit + integration tests passed (client roster, proxy server,
  UDP probe).

---

Binaries: signed Android APK (versionCode 45) + Windows win64 zip, both
built and published by GitHub Actions from this tag. `SHA256SUMS.txt` lists
the asset checksums; the Source code archives are generated automatically
by GitHub.
