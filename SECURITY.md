# Security Policy

## Local Service Scope (Android Host)

- The SOCKS5 proxy (TCP CONNECT / UDP ASSOCIATE, default port 8080) and the HTTP
  proxy (port 8081) are **bound to all interfaces (0.0.0.0) for network-interface
  enumeration compatibility**, but they **only accept connections from the
  loopback range (127.0.0.0/8, for self-testing) and from the Wi-Fi Direct group
  owner /24 subnet**. The proxies do not start until a group is formed.
- The Wi-Fi passphrase is never shown in the notification shade. It is randomly
  generated on first launch and persisted; it can only be viewed or changed in
  the settings page, and it is excluded from cloud backup and device migration.
- SOCKS username/password authentication is not implemented: source filtering
  narrows the attack surface but is not identity authentication. Do not bridge
  the host to other networks and use it as a public proxy on untrusted networks.

## Permissions (Windows Client)

- Administrator privileges are required for: creating the Wintun virtual
  adapter, modifying local routes (limited to the /1 route pointing at this
  program's gateway 10.0.0.1), and enabling/disabling ICS sharing (only when
  the user clicks "Share to game console").
- On stop/exit, the routes and ICS state added by this program are rolled back;
  routes belonging to other VPNs are never deleted or modified.
- A normal installation does not download any binaries from the network; the
  fallback download requires user confirmation and enforces SHA-256 verification.

## Reporting a Vulnerability

- Please report security issues through the project's Issue tracker, including
  minimal reproduction steps and the scope of impact.

## Antivirus / SmartScreen False Positives

- This program is packaged with PyInstaller, requests administrator privileges,
  and modifies system routes — these genuine behaviors may trigger
  "unknown publisher" warnings. Please verify the SHA-256 manifest of the
  release package before running; do not disable Defender for this reason.
- If you confirm a false positive, appeal through Microsoft's official sample
  submission channel with the file hash and build records.
