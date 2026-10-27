// LinkHelm — Windows network control (C++ rewrite P3).
// Replaces the netsh-spawning parts of console_bridge.py: adapter census,
// stale WLAN-profile sweep, hosts-file integrity check, read-only view of
// the system proxy (we never write it — coexistence with Clash Verge).
#pragma once

#include <windows.h>

#include <functional>
#include <string>
#include <vector>

namespace linkhelm {

struct AdapterInfo {
    std::wstring name;
    unsigned long type = 0;      // IF_TYPE_*
    bool is_tun = false;         // 131 (TUN) or 53 (prop/virtual, LinkHelm-IwIP)
    bool is_wifi = false;        // 71
};

struct HostsReport {
    std::wstring path;
    long long bom_count = 0;     // stray U+FEFF — 电脑管家 flags this
    long long size = 0;
    std::vector<std::string> active_entries;
};

// Read-only census via GetAdaptersAddresses (no process spawning).
std::vector<AdapterInfo> AdapterCensus();
std::wstring CensusLine(const std::vector<AdapterInfo>& adapters);

// Removes drifted clones of the LinkHelm WLAN profiles ("LinkHelm 31",
// "LinkHelm-7", ...) and returns how many were deleted. Never touches the
// profile of a network currently in use, and does nothing at all while a
// LinkHelm network is live — that is what broke 1.0.55.
int SweepStaleWlanProfiles(const std::wstring& base_name);

HostsReport InspectHosts();
bool RepairHostsBom();

// Read-only: what proxy WinINET currently points at (belongs to Clash Verge).
bool ReadSystemProxy(bool* enabled, std::wstring* server);

// Local helper: run a command and capture its UTF-8 output.
bool RunCommand(const std::wstring& cmd, std::string* out);

// ---------------------------------------------------------------------------
// beta1.0.71 — ported from the VERIFIED python fixes in main.py (the COM
// slot numbers, the rename semantics and the registry store layout were all
// confirmed live on this machine; see main.py sweep_ghost_linkhelm_networks).
// ---------------------------------------------------------------------------

// The network flyout ghosts ("LinkHelm 2".."LinkHelm 37") are NOT WLAN
// profiles — they are net-connection-profiles of the app's own lwIP adapter,
// one drifted clone per session. This sweep:
//   1. renames the CONNECTED drift to the fixed "LinkHelm" (INetwork::SetName)
//      so the numbering can never start again;
//   2. deletes DISCONNECTED ghosts from the NetworkList registry store
//      (HKLM/.../NetworkList Profiles + Signatures) — INetwork has no Delete
//      method, the store is the only way (needs administrator rights, which
//      the app already holds for its routes).
// "DIRECT-LinkHelm" (the phone's Wi-Fi Direct network) is NEVER touched.
// Returns the number of deleted ghosts; log_fn receives progress lines.
int SweepGhostNetworks(
    const std::function<void(const std::wstring&)>& log_fn = nullptr);

// While a session is live, NCSI must probe a DOMESTIC endpoint (Baidu):
// Microsoft's own probe hosts are unreachable without the tunnel, so the
// flyout kept saying "无法访问 Internet" even when everything worked. The
// original values are stashed for RestoreNcsiProbe(). Needs admin rights.
bool SetDomesticNcsiProbe(
    const std::function<void(const std::wstring&)>& log_fn = nullptr);

// Put the original NCSI probe host/path/content back (every exit path).
bool RestoreNcsiProbe(
    const std::function<void(const std::wstring&)>& log_fn = nullptr);

// beta1.0.81 rebuild: NCSI caches per-network captive-portal verdicts; a
// network already flagged stays flagged until the service re-probes. Force
// that immediately after writing the new probe values.
void RestartNlasvc(const std::function<void(const std::wstring&)>& log_fn = nullptr);

// beta1.0.8 display fix: crashed 1.0.55..1.0.72 builds left
// EnableActiveProbing=0 behind — with probing off NLA never validates
// anything and EVERY network shows "无法访问 Internet" forever. Remember
// the original first, force probing ON at launch, restore at exit.
void RememberNcsiOriginal();
bool ForceNcsiProbeOn(
    const std::function<void(const std::wstring&)>& log_fn = nullptr);
bool RestoreNcsiOriginal(
    const std::function<void(const std::wstring&)>& log_fn = nullptr);

}  // namespace linkhelm
