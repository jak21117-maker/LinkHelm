# LinkHelm 1.1.0

First stable release. Both ends ship the same version number: **1.1.0**
(Android versionCode 62).

## Highlights

**Native C++ desktop edition.** The Windows app was rewritten in C++17 on
the raw Win32 API — same interface, same behavior, one ~0.9 MB executable
with instant startup. The install package shrinks from ~200 MB to ~40 MB.
The legacy Python edition remains in the repository under `laptop_proxy/`
for study and comparison.

**Store accelerator, rebuilt subscription chain.** Downloads now try the
system proxy, your own proxy, any live local proxy port (auto-detected),
and a direct connection in order — so subscriptions fetch reliably no
matter how your network is set up. Panels with multiple front domains
fail over automatically, and responses are gzip-decoded with real error
reporting.

**Latency test, now accurate.** Node names containing Chinese characters
or emoji are correctly UTF-8 encoded before querying the kernel, so every
node reports its true delay. The bundled kernel now also parses
share-link subscriptions (`vless://` `ss://` `vmess://` `trojan://`
`hysteria2://`) directly.

**Fully bilingual interface.** Every status message, dialog and node name
follows the selected language — English shows English node names,
Chinese shows the originals.

## Measured performance across versions

Same phone, same PC, same Wi-Fi Direct link, same game server, one
client, same time window. Throughput is expressed as a percentage of the
host machine's direct line speed.

| Metric | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| Throughput vs host line speed | ~30% | ~50% | ~60% | **70–80%** |
| Typical latency | 100–200 ms | ~100 ms | 30–80 ms (occasionally 100) | **20–70 ms** |

The long climb came from replacing the legacy tun2socks core with the
native lwIP engine (beta1.0.56) and then tuning buffers, connection
lifecycle and DNS handling in the C++ edition.

## File transfer & store boost evolution

| Capability | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| File transfer | manual, no resume | real file names kept | resumable, SHA-256 verified | **resumable + real names + 10 GB + live sync status** |
| Store boost | — | — | mihomo kernel, Clash YAML only | **any subscription format, auto proxy detection, latency test, bilingual** |

## Verification

- Desktop: end-to-end test against a live subscription URL — download,
  base64/share-link conversion, kernel parse, 20 nodes listed, stable
  90-second run; fresh 1.1.0 EXE passes the startup smoke test.
- Android: clean build (versionCode 62); file-transfer sync reliability
  hardened; full review found no crash-level issues.
