# LinkHelm 1.1.0

First stable release. Both ends ship the same version number: **1.1.0**
(Android versionCode 62). The Windows desktop app is now the **native C++
edition** — same UI, same behavior, one ~0.9 MB executable instead of the
Python runtime bundle. The legacy Python edition remains in the repository
under `laptop_proxy/` for study and comparison.

## Desktop: native C++ edition

- The whole desktop app was rewritten in C++17 (Win32 API directly, no UI
  framework). Functionality is unchanged; startup is instant and the
  package shrinks from ~200 MB to ~40 MB.
- The lwIP engine (hev-socks5-tunnel + Wintun), the mihomo kernel and all
  helper modules are the same pinned, SHA-256-verified binaries as before.

## Store accelerator (both ends)

- Subscription download chain rebuilt: system proxy → user proxy → local
  port probe (11 common ports, 300 ms) → direct, plus automatic sibling
  domain failover for multi-frontend panels. No more blind port guessing.
- Latency test fixed: node names are now correctly UTF-8 percent-encoded
  (Chinese/emoji names previously broke the kernel API with 404s).
- Share-link subscriptions (vless/ss/vmess/trojan/hysteria2) are converted
  in-process; the bundled mihomo kernel parses them directly.
- Full bilingual UI: every status message is translated; node names are
  shown in English when the interface language is English.

## Measured performance across versions

Same phone, same PC, same Wi-Fi Direct link, same game server, one client,
same time window. Throughput is expressed as a percentage of the host
machine's direct line speed.

| Metric | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| Throughput vs host line speed | ~30% | ~50% | ~60% | **70–80%** |
| Typical latency | 100–200 ms | ~100 ms | 30–80 ms | **20–70 ms** |

The long climb from beta1.0.0 to 1.1.0 came from replacing the legacy
tun2socks core with the native lwIP engine (beta1.0.56) and then tuning
buffers, connection lifecycle and DNS handling in the C++ edition.

## File transfer & store boost evolution

| Capability | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| File transfer | manual, no resume | real file names kept | resumable, SHA-256 verified | **resumable + real names + 10 GB + live sync status** |
| Store boost | — | — | mihomo kernel, Clash YAML only | **any subscription format, auto proxy detection, latency test, bilingual** |

## Verification

- Desktop: end-to-end test on a live subscription URL — download →
  base64/vless conversion → kernel parse → 20 nodes listed → stable
  90-second run. Smoke test: fresh 1.1.0 EXE starts and stays alive.
- Android: debug APK builds clean (versionCode 62); file-transfer
  sync wrapped in runCatching; no crash-level findings in review.
