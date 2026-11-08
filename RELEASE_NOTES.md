# LinkHelm 1.1.3

Maintenance release following **1.1.0**, the previous published version. The
Windows desktop application and the Android client both ship **1.1.3**
(`versionCode = 65`). Changes delivered in the intervening internal builds
are consolidated here, since those builds were never published. The artifacts
attached to this tag are produced by the repository CI workflow from the
sources in this tree.

## Changes since 1.1.0

### Connection establishment

Resolver preference is unified across the desktop UDP probe pool, the Android
VPN builder and the TUN adapter's static DNS configuration
(`119.29.29.29` → `223.5.5.5` → `114.114.114.114`). The desktop probe pool
was restored to three candidates after the C++ rewrite had reduced it to a
single resolver; each candidate is probed independently and its outcome is
reported separately.

Probe and discovery intervals were recalibrated against measured link
behaviour. SOCKS5 precheck attempts use a 0.8 s timeout with a 150 ms
inter-attempt interval; TUN adapter discovery polls at 250 ms; DNS
verification and route readback retries were shortened in proportion. Route
pinning, the explicit interface binding and every readback assertion are
unchanged. On the test host, failure detection against an unreachable
endpoint completed in **2.82 s**.

### Route handling and shutdown

The `/1` route readback was corrected for zh-CN `route print` output, where
the interface column carries the TUN address rather than the gateway column.
Validation accepts either column as evidence of a route bound to
`10.0.0.1` / `10.0.0.2`, with publication retries and diagnostic logging of
the values actually observed. Teardown is idempotent, the monitor is
interruptible, and route cleanup plus shutdown were moved off the UI thread
with expedited window destruction. Shutdown on an idle link completes in
**0.56 s** on the test host.

### Reconnection control

The governed retry loop is bounded at ten consecutive failures with a 2 s
inter-attempt interval, and accepts a user-initiated disconnect at any point.
An atomic abort flag is evaluated at each iteration and throughout the
inter-attempt wait; the Disconnect control remains enabled for the complete
retry cycle. Aborting emits
`[reconnect] stopped (user disconnect / shutdown)` and terminates further
attempts.

### Interface

The inbound form is laid out as a two-row grid: phone address, port and the
UDP verification control share the first row; credentials and the visibility
toggle occupy the second. Rendering artefacts at the corners of rounded
regions — produced where the system's rectangular background erase extended
past the region fill — are eliminated by repainting only the four corner
quadrants, leaving child controls outside that pass. A named mutex guards
against a second instance, with a dialog whose title, text and button label
follow the selected interface language rather than the OS locale.

### Diagnostics

A failed SOCKS5 precheck now identifies an unavailable WLAN / hotspot link as
a probable cause alongside the endpoint and credential checks.

## Measured performance across versions

Same phone, same PC, same Wi-Fi Direct link, same game server, one client,
same time window. Throughput is expressed as a percentage of the host
machine's direct line speed.

| Metric | beta1.0.0 | beta1.0.3 | beta1.0.56 | 1.1.0 / 1.1.3 |
|---|---|---|---|---|
| Throughput vs host line speed | ~30% | ~50% | ~60% | **70–80%** |
| Typical latency | 100–200 ms | ~100 ms | 30–80 ms (occasionally 100) | **20–70 ms** |

The 1.1.x maintenance builds did not alter the lwIP engine, buffer sizing or
route pinning, so throughput and latency remain at the 1.1.0 measured values.
The increase over the earlier betas came from replacing the legacy tun2socks
core with the native lwIP engine (beta1.0.56) and subsequently tuning
buffers, connection lifecycle and DNS handling in the C++ edition.

## Verification

- Desktop: controlled runtime measurements on the same host covering layout
  geometry, connection-failure detection, retry abort and shutdown latency.
  A smoke test was also run from the delivered folder in both language modes,
  including the single-instance dialog and process lifetime.
- Android: clean build, `versionCode = 65`.
- The live UDP data plane against a paired phone was not exercised on this
  host; that verification remains an on-device step.
