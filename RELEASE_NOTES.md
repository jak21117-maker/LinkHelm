## LinkHelm beta1.0.3

Three iterations of capacity engineering since beta1.0.0, capped by a  
field-measured recalibration of the whole session architecture and a  
security-hardened file-transfer channel.

### Connection architecture evolution

- **Adaptive session profiles** (introduced beta1.0.1, retuned through  
  beta1.0.3): NORMAL / TURBO modes now govern TCP sessions, UDP associations,  
  per-device quotas and queues as one coordinated set, switchable live with  
  no share restart.
- **Field-calibrated capacity** (beta1.0.3, measured TCP:UDP ≈ 3:1): NORMAL  
  carries 320 global TCP sessions / 160 per device; TURBO carries 700 global  
  sessions for multi-device hosts (e.g. 5 devices × ~140 ≈ 700).
- **Dynamic per-device quota in TURBO**: 120-170 new-session allowance,  
  stepped ±10 every 30 s from measured utilization — deliberately slow, never  
  random jitter, so devices never see flapping ceilings.
- **Fair sharing**: with 3+ devices the per-device quota becomes a fair share  
  of the global budget, floored at 100 under crowding (6-7 devices settle  
  around 100).
- **Per-device UDP follows the 3:1 rule**: a device's UDP ceiling is 1/3 of  
  its live TCP quota, tightening automatically under pressure (a PC game  
  holds 30-50 UDP endpoints, a phone/tablet game 20-30).
- **PROTECT / RECOVER**: sessions ≥95% of the cap for 15 s engage extreme-  
  pressure protection (new admission 100; established sessions untouched);  
  ≤80% for 120 s restores normal admission.
- **UDP lifecycle management** (beta1.0.1): UDP associations carry their own  
  admission budget, and idle associations are reclaimed (300 s standard /  
  600 s turbo) instead of holding relay sockets forever.
- **TCP idle reclaim** (beta1.0.3): established-but-idle sessions retire  
  after 3 h (6 h in turbo); every real send/receive refreshes the timer.
- **Compatibility micro-kernel** (beta1.0.3): third-party client support  
  (Karing/Streisand) keeps only the relaxed UDP source-endpoint tracking it  
  actually needs, with zero capacity trade-off.
- **Link monitoring** (beta1.0.2): lane failover uses strike accumulation  
  plus a 6-second confirm probe before switching, and the Android watchdog  
  self-heals on a 5-minute cadence.

### File transfer with IP pairing (new since beta1.0.0)

- Peer-to-peer file transfer between the phone and the PC over the Wi-Fi  
  Direct link — no internet, no cloud. Single files up to 1 GB, streamed  
  with SHA-256 integrity verification on both ends; entries live on the home  
  screen of both apps.
- **Mutual IP pairing** (beta1.0.3): transfers are only possible between  
  paired devices — both sides see the other's actual address, tap Pair, and  
  the pairing persists on each end.
- **Actual local IP display** (beta1.0.3): both UIs show the device's real  
  Wi-Fi Direct address (dynamic — it changes when the group is rebuilt).
- The desktop dialog gained a peer-IP field with a Pair button; sending is  
  disabled until paired, and refusals state the specific reason (pairing,  
  credentials, busy).

### Diagnostics

- The desktop log annotates tunnel failures with the corresponding cause  
  (host-side admission, credentials, link state) and translates SOCKS5 reply  
  codes into their RFC 1928 meanings — no more bare "rejected" lines.

### UI & platform polish

- SOCKS5 password fields gained show/hide toggles on both platforms  
  (beta1.0.1).
- Home footers updated to MIT-consistent wording with a repository link  
  (beta1.0.1).
- Turbo mode copy simplified, with a recommendation hint once 3+ devices are  
  connected; the iPhone entry became "iOS proxy-free networking (no app)"  
  with an HTTP-usage note; settings panel hardened for customtkinter 5.2.2  
  (beta1.0.2/1.0.3).

### Packaging

- Windows build ships pinned tun2socks v2.7.0 and Wintun 0.14.1 binaries.
- Source archive slimmed to ~36 MB by excluding build artifacts.

Binaries: signed Android APK (versionCode 37) + Windows win64 zip.  
SHA256SUMS.txt included.
