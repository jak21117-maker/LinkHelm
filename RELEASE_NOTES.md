# LinkHelm 1.1.3

Maintenance release. The Windows desktop application and the Android client
both ship **1.1.3** (`versionCode = 65`). The artifacts attached to this tag
are produced by the repository CI workflow from the sources in this tree.

## Changes

### Connection establishment

Probe and discovery intervals were recalibrated against measured link
behaviour. SOCKS5 precheck attempts now use an 0.8 s timeout instead of
1.5 s, with a 150 ms inter-attempt interval; TUN adapter discovery polls at
250 ms rather than 1 s; DNS verification and route readback retries were
shortened in proportion. Route pinning, the explicit interface binding and
every readback assertion are unchanged. Against an unreachable endpoint,
failure detection completed in **2.82 s**, compared with **5.04 s** for
1.1.2 under an identical A/B procedure on the same host.

### Reconnection control

The governed retry loop now accepts a user-initiated disconnect at any
point. An atomic abort flag is evaluated at each iteration and throughout
the inter-attempt wait, and the Disconnect control remains enabled for the
complete retry cycle. Aborting emits
`[reconnect] stopped (user disconnect / shutdown)` and terminates further
attempts. The 10-consecutive-failure ceiling introduced in 1.1.2 is
retained; the inter-attempt interval is 2 s. Shutdown on an idle link
completes in **0.56 s** (1.1.2: 4.43 s).

### Interface

The inbound form is laid out as a two-row grid: phone address, port and the
UDP verification control share the first row; credentials and the
visibility toggle occupy the second. Rendering artefacts at the corners of
rounded regions — produced where the system's rectangular background erase
extended past the region fill — are eliminated by repainting only the four
corner quadrants, leaving child controls outside that pass.

### Diagnostics

A failed SOCKS5 precheck now identifies an unavailable WLAN / hotspot link
as a probable cause alongside the endpoint and credential checks.

## Earlier maintenance in this series

- **1.1.1** — Resolver preference unified across the desktop probe pool, the
  Android VPN builder and the TUN adapter's static DNS configuration
  (`119.29.29.29` → `223.5.5.5` → `114.114.114.114`). The desktop UDP probe
  pool was restored to three candidates after the C++ rewrite had reduced it
  to a single resolver.
- **1.1.2** — Shutdown and route teardown moved off the UI thread; automatic
  disconnect after ten consecutive failures; single-instance guard with a
  language-aware dialog; route readback corrected for zh-CN `route print`
  output, where the interface column carries the TUN address.

## Measured performance across versions

Same phone, same PC, same Wi-Fi Direct link, same game server, one client,
same time window. Throughput is expressed as a percentage of the host
machine's direct line speed.

| Metric | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 |
|---|---|---|---|---|
| Throughput vs host line speed | ~30% | ~50% | ~60% | **70–80%** |
| Typical latency | 100–200 ms | ~100 ms | 30–80 ms (occasionally 100) | **20–70 ms** |

The increase came from replacing the legacy tun2socks core with the native
lwIP engine (beta1.0.56) and subsequently tuning buffers, connection
lifecycle and DNS handling in the C++ edition.

## Verification

- Desktop: A/B runtime comparison against the 1.1.2 deliverable on the same
  host, covering layout geometry, connection-failure detection, retry abort
  and shutdown latency. A smoke test was also run from the delivered folder
  in both language modes, including the single-instance dialog and process
  lifetime.
- Android: clean `assembleDebug` build, `versionCode = 65`.
- The live UDP data plane against a paired phone was not exercised on this
  host; that verification remains an on-device step.
