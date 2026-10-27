// LinkHelm tunnel manager (beta1.0.85) — pure C++ connect flow.
// Engine (hev-socks5-tunnel) runs inside OUR job object: UI dies -> engines
// die, kernel-guaranteed. netsh/route calls are direct ANSI-safe captures.

#include "tun_manager.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <shellapi.h>
#include <shlobj.h>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <fstream>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "iphlpapi.lib")

namespace linkhelm {
namespace {

constexpr wchar_t kAdapterName[] = L"LinkHelm-IwIP";
constexpr wchar_t kAdapterGuid[] = L"F4E4A1B2-9C3D-4E5F-8A7B-6C5D4E3F2A1B";
constexpr char kTunGateway[] = "10.0.0.1";
constexpr char kTunLocalIp[] = "10.0.0.2";

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), &out[0], n,
                        nullptr, nullptr);
    return out;
}
std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                                nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n);
    return out;
}
std::string AnsiToUtf8(const std::string& a) {
    if (a.empty()) return {};
    int w = MultiByteToWideChar(CP_ACP, 0, a.c_str(), -1, nullptr, 0);
    std::wstring ws((size_t)(w ? w - 1 : 0), L'\0');
    if (w) MultiByteToWideChar(CP_ACP, 0, a.c_str(), -1, &ws[0], w);
    return Utf8(ws);
}

int RunCapture(const std::wstring& cmdline, std::string& out,
               DWORD timeoutMs = 20000) {
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    HANDLE rd = nullptr, wr = nullptr;
    if (!CreatePipe(&rd, &wr, &sa, 0)) return -1;
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = wr;
    si.hStdError = wr;
    PROCESS_INFORMATION pi{};
    std::wstring cmd = L"cmd.exe /c " + cmdline;
    BOOL ok = CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, TRUE,
                             CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
    CloseHandle(wr);
    if (!ok) { CloseHandle(rd); return -1; }
    out.clear();
    char buf[4096];
    DWORD n = 0;
    while (ReadFile(rd, buf, sizeof(buf), &n, nullptr) && n) out.append(buf, n);
    CloseHandle(rd);
    WaitForSingleObject(pi.hProcess, timeoutMs);
    DWORD rc = 0;
    GetExitCodeProcess(pi.hProcess, &rc);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)rc;
}

int Run(const std::wstring& c) {
    std::string out;
    return RunCapture(c, out);
}

std::wstring Q(const std::wstring& s) { return L"\"" + s + L"\""; }

// gateways currently serving dest/mask (route print, ANSI->UTF8, strict cols)
std::vector<std::string> RouteGateways(const std::string& dest,
                                       const std::string& mask) {
    std::vector<std::string> out;
    std::string raw;
    RunCapture(L"route print -4", raw);
    std::string txt = AnsiToUtf8(raw);
    size_t ls = 0;
    while (ls < txt.size()) {
        size_t le = txt.find('\n', ls);
        if (le == std::string::npos) le = txt.size();
        size_t t = txt.find_first_not_of(" \t", ls);
        if (t != std::string::npos && t < le) {
            std::string line = txt.substr(t, le - t);
            if (line.rfind(dest + " ", 0) == 0) {
                int tok = 0;
                size_t q = 0;
                std::string gw;
                while (tok <= 2 && q < line.size()) {
                    while (q < line.size() && line[q] == ' ') ++q;
                    size_t s = q;
                    while (q < line.size() && line[q] != ' ') ++q;
                    if (tok == 1 && line.substr(s, q - s) != mask) break;
                    if (tok == 2) gw = line.substr(s, q - s);
                    ++tok;
                }
                bool ip = !gw.empty() && gw.find('.') != std::string::npos &&
                          gw.find_first_not_of("0123456789.") ==
                              std::string::npos;
                bool dup = false;
                for (auto& g : out) if (g == gw) dup = true;
                if (ip && !dup) out.push_back(gw);
            }
        }
        ls = le + 1;
    }
    return out;
}

bool ListContains(const std::vector<std::string>& v, const char* s) {
    for (auto& g : v) if (g == s) return true;
    return false;
}

std::wstring EngineDir() {
    wchar_t lad[MAX_PATH]{};
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_LOCAL_APPDATA, nullptr, 0,
                                   lad))) {
        std::wstring d = std::wstring(lad) + L"\\LinkHelm\\engine";
        CreateDirectoryW(d.c_str(), nullptr);
        return d;
    }
    return L".";
}

}  // namespace

struct TunnelManager::Impl {
    HANDLE job = nullptr;   // kill-on-close; engine lives inside
};

TunnelManager::TunnelManager(LogFn log, StateFn status)
    : log_(std::move(log)), status_(std::move(status)), impl_(new Impl) {}

TunnelManager::~TunnelManager() {
    Stop();
    if (impl_->job) CloseHandle(impl_->job);
    delete impl_;
}

bool TunnelManager::PrecheckSocks5(const TunnelConfig& cfg,
                                   std::wstring* error) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)cfg.phone_port);
    inet_pton(AF_INET, Utf8(cfg.phone_ip).c_str(), &addr.sin_addr);
    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);
    connect(s, (sockaddr*)&addr, sizeof(addr));
    fd_set w;
    FD_ZERO(&w);
    FD_SET(s, &w);
    timeval tv{5, 0};
    // beta1.0.86 fix: writability alone is NOT success — a failed connect
    // also marks the socket writable. Read SO_ERROR for the real verdict.
    bool ok = false;
    if (select(0, nullptr, &w, nullptr, &tv) > 0) {
        int soerr = 0;
        int len = sizeof(soerr);
        getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&soerr, &len);
        ok = (soerr == 0);
    }
    closesocket(s);
    if (!ok && error) {
        *error = L"phone proxy precheck failed (" + cfg.phone_ip + L":" +
                 std::to_wstring(cfg.phone_port) + L"): check the hotspot link and credentials";
    }
    return ok;
}

bool TunnelManager::WriteEngineConfig(const TunnelConfig& cfg,
                                      std::wstring* path) {
    std::wstring dir = EngineDir();
    *path = dir + L"\\main.yml";
    std::wstring yaml = L"tunnel:\n  name: " + std::wstring(kAdapterName) +
        L"\n  guid: " + kAdapterGuid +
        L"\n  mtu: 1420\n  ipv4: 10.0.0.1\n  ipv6: 'fc00::1'\n  icmp: 'off'\n"
        L"socks5:\n  address: '" + cfg.phone_ip + L"'\n  port: " +
        std::to_wstring(cfg.phone_port) +
        L"\n  udp: 'udp'\n  udp-address: '" + cfg.phone_ip + L"'\n";
    if (!cfg.socks_user.empty())
        yaml += L"  username: '" + cfg.socks_user + L"'\n";
    if (!cfg.socks_pass.empty())
        yaml += L"  password: '" + cfg.socks_pass + L"'\n";
    yaml += L"misc:\n  connect-timeout: 25000\n"
            L"  tcp-read-write-timeout: 300000\n"
            L"  udp-read-write-timeout: 60000\n  max-session-count: 0\n  log-level: info\n";
    FILE* f = _wfopen(path->c_str(), L"wb");
    if (!f) return false;
    std::string yaml8 = Utf8(yaml);
    fwrite(yaml8.c_str(), 1, yaml8.size(), f);
    fclose(f);
    return true;
}

bool TunnelManager::LaunchEngine(const std::wstring& cfg_path,
                                 std::wstring* error) {
    if (!impl_->job) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION jli{};
        jli.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        impl_->job = CreateJobObjectW(nullptr, nullptr);
        SetInformationJobObject(impl_->job,
                                JobObjectExtendedLimitInformation, &jli,
                                sizeof(jli));
    }
    // engine exe: next to this exe, engine\hev-socks5-tunnel.exe
    wchar_t self[MAX_PATH]{};
    GetModuleFileNameW(nullptr, self, MAX_PATH);
    std::wstring d(self);
    auto slash = d.rfind(L'\\');
    if (slash != std::wstring::npos) d = d.substr(0, slash);
    std::wstring exe = d + L"\\engine\\hev-socks5-tunnel.exe";
    std::wstring cmd = L"\"" + exe + L"\" \"" + cfg_path + L"\"";
    // engine stdout -> log
    std::wstring logp = EngineDir() + L"\\engine_stdout.log";
    HANDLE lf = CreateFileW(logp.c_str(), GENERIC_WRITE, FILE_SHARE_READ,
                            nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL,
                            nullptr);
    if (lf != INVALID_HANDLE_VALUE)
        SetFilePointer(lf, 0, nullptr, FILE_END);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = lf;
    si.hStdError = lf;
    PROCESS_INFORMATION pi{};
    BOOL ok = CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, TRUE,
                             CREATE_SUSPENDED | CREATE_NO_WINDOW, nullptr,
                             EngineDir().c_str(), &si, &pi);
    if (lf != INVALID_HANDLE_VALUE) CloseHandle(lf);
    if (!ok) {
        if (error) *error = L"engine start failed (hev-socks5-tunnel)";
        return false;
    }
    AssignProcessToJobObject(impl_->job, pi.hProcess);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    engine_ = pi.hProcess;
    engine_pid_ = pi.dwProcessId;
    return true;
}

bool TunnelManager::WaitAdapter(int seconds, std::wstring* conn_name) {
    std::string out;
    for (int i = 0; i < seconds; ++i) {
        if (engine_ && WaitForSingleObject(engine_, 0) == WAIT_OBJECT_0)
            return false;
        RunCapture(L"netsh interface ipv4 show interfaces", out);
        std::string txt = AnsiToUtf8(out);
        size_t pos = 0;
        while ((pos = txt.find('\n', pos)) != std::string::npos) {
            size_t eol = txt.find('\n', pos + 1);
            std::string line = txt.substr(
                pos + 1, eol == std::string::npos
                             ? std::string::npos : eol - pos - 1);
            std::string low = line;
            std::transform(low.begin(), low.end(), low.begin(), ::tolower);
            if (low.find("linkhelm") != std::string::npos) {
                // connection name = tokens after column 4 (idx met mtu state)
                size_t sp = 0;
                int tok = 0;
                while (tok < 4 && sp < line.size()) {
                    while (sp < line.size() && line[sp] == ' ') ++sp;
                    while (sp < line.size() && line[sp] != ' ') ++sp;
                    ++tok;
                }
                while (sp < line.size() && line[sp] == ' ') ++sp;
                if (sp < line.size()) {
                    std::string nm = line.substr(sp);
                    nm.erase(nm.find_last_not_of(" \r\n\t") + 1);
                    *conn_name = Utf16(nm);
                    return true;
                }
            }
            pos = eol == std::string::npos ? txt.size() : eol;
        }
        Sleep(1000);
    }
    return false;
}

bool TunnelManager::ConfigureAdapter(const std::wstring& conn_name) {
    std::wstring n = Q(conn_name);
    Run(L"netsh interface ipv4 set address name=" + n +
        L" static 10.0.0.2 255.255.255.0 10.0.0.1 1");
    // beta1.0.88: user order 119/223/114 as statics; then PROBE the phone
    // side: query each candidate via the phone SOCKS5 UDP relay and REORDER
    // primary to the first that answers (host-consistent on networks whose
    // DHCP DNS hijacks/redirects - the field instability source).
    Run(L"netsh interface ipv4 set dns name=" + n +
        L" static 119.29.29.29 primary validate=no");
    Run(L"netsh interface ipv4 add dns name=" + n +
        L" 223.5.5.5 index=2 validate=no");
    Run(L"netsh interface ipv4 add dns name=" + n +
        L" 114.114.114.114 index=3 validate=no");
    Run(L"ipconfig /flushdns");
    // beta1.0.9: STRICT order per user — 119 primary, 223 second, 114
    // third. beta1.0.92: netsh set dns is asynchronous on some builds — the
    // immediate readback can still show the OLD primary (the field "DNS
    // still 223 primary" report). Retry the readback up to 3x, and RE-SET
    // the static order if the readback disagrees.
    if (log_) log_(L"DNS set: 119.29.29.29 (primary), 223.5.5.5, 114.114.114.114");
    for (int attempt = 0; attempt < 3; ++attempt) {
        std::string chk;
        RunCapture(L"netsh interface ipv4 show dnsservers name=" + n, chk);
        std::string txt = AnsiToUtf8(chk);
        if (txt.find("119.29.29.29") != std::string::npos &&
            txt.find("119.29.29.29") < txt.find("223.5.5.5")) {
            break;  // order verified
        }
        if (log_) log_(L"DNS verify: retrying (" + std::to_wstring(attempt + 1) + L")");
        Sleep(700);
        Run(L"netsh interface ipv4 set dns name=" + n +
            L" static 119.29.29.29 primary validate=no");
        Run(L"netsh interface ipv4 add dns name=" + n +
            L" 223.5.5.5 index=2 validate=no");
        Run(L"netsh interface ipv4 add dns name=" + n +
            L" 114.114.114.114 index=3 validate=no");
    }

    // verify (a drifted name makes every netsh call a silent no-op)
    std::string out;
    RunCapture(L"netsh interface ipv4 show dnsservers name=" + n, out);
    std::string txt = AnsiToUtf8(out);
    if (txt.find("223.5.5.5") == std::string::npos &&
        txt.find("119.29.29.29") == std::string::npos &&
        txt.find("114.114.114.114") == std::string::npos) {
        if (log_)
            log_(L"WARNING: no DNS active on the tunnel adapter - name "
                 L"resolution will fail (drifted connection name?)");
        return false;
    }
    return true;
}

bool TunnelManager::AddRoutes(const std::wstring& conn_name,
                              std::wstring* error) {
    // stale default via TUN? remove (metric 1 beats WLAN)
    auto def = RouteGateways("0.0.0.0", "0.0.0.0");
    if (ListContains(def, kTunGateway)) {
        Run(L"route delete 0.0.0.0 mask 0.0.0.0 10.0.0.1");
        if (log_) log_(L"removed stale TUN default route from a previous run");
    }
    // return route to phone via original default gw
    if (!def.empty() && !phone_ip_.empty()) {
        std::string gw = def[0];
        std::wstring wgw = Utf16(gw);
        std::wstring rc = L"route add " + phone_ip_ +
                          L" mask 255.255.255.255 " + wgw + L" metric 1";
        if (Run(rc) == 0) {
            phone_route_gw_ = wgw;
            if (log_) log_(L"return route to phone pinned via " + wgw);
        }
    }
    // TUN interface index
    int tunIdx = 0;
    {
        std::string out;
        RunCapture(L"netsh interface ipv4 show interfaces", out);
        std::string txt = AnsiToUtf8(out);
        size_t pos = 0;
        while ((pos = txt.find('\n', pos)) != std::string::npos) {
            size_t eol = txt.find('\n', pos + 1);
            std::string line = txt.substr(
                pos + 1, eol == std::string::npos
                             ? std::string::npos : eol - pos - 1);
            std::string low = line;
            std::transform(low.begin(), low.end(), low.begin(), ::tolower);
            if (low.find("linkhelm") != std::string::npos) {
                tunIdx = atoi(line.c_str());
                if (tunIdx > 0) break;
            }
            pos = eol == std::string::npos ? txt.size() : eol;
        }
    }
    for (const char* destC : {"0.0.0.0", "128.0.0.0"}) {
        std::string dest = destC;
        auto existing = RouteGateways(dest, "128.0.0.0");
        if (ListContains(existing, kTunGateway)) {
            Run(L"route delete " + Utf16(dest) + L" mask 128.0.0.0 10.0.0.1");
            existing = RouteGateways(dest, "128.0.0.0");
        }
        if (!existing.empty()) {
            if (log_) log_(L"route " + Utf16(dest) + L"/1 exists, skipped");
            continue;
        }
        std::wstring cmd = L"route add " + Utf16(dest) +
                           L" mask 128.0.0.0 10.0.0.1 metric 1";
        if (tunIdx) cmd += L" IF " + std::to_wstring(tunIdx);
        int rc = Run(cmd);
        if (rc != 0 && tunIdx) {
            rc = Run(L"route add " + Utf16(dest) +
                     L" mask 128.0.0.0 10.0.0.1 metric 1");
        }
        if (rc != 0) {
            if (error)
                *error = L"route add " + Utf16(dest) + L"/1 failed: another VPN?";
            CleanRoutes();
            return false;
        }
        // readback verify: gateway line exists
        auto after = RouteGateways(dest, "128.0.0.0");
        if (!ListContains(after, kTunGateway)) {
            if (error) *error = L"route " + Utf16(dest) + L"/1 readback verify failed";
            CleanRoutes();
            return false;
        }
    }
    return true;
}

void TunnelManager::CleanRoutes() {
    Run(L"route delete 0.0.0.0 mask 128.0.0.0 10.0.0.1");
    Run(L"route delete 128.0.0.0 mask 128.0.0.0 10.0.0.1");
    Run(L"route delete 0.0.0.0 mask 0.0.0.0 10.0.0.1");
    if (!phone_route_gw_.empty() && !phone_ip_.empty())
        Run(L"route delete " + phone_ip_ + L" mask 255.255.255.255 " +
            phone_route_gw_);
    phone_route_gw_.clear();
}

void TunnelManager::MonitorLoop() {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    while (monitoring_.load()) {
        Sleep(5000);
        if (!monitoring_.load()) break;
        if (engine_ && WaitForSingleObject(engine_, 0) == WAIT_OBJECT_0) {
            if (status_) status_(L"Status: engine exited unexpectedly");
            continue;
        }
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons((u_short)_wtoi(phone_port_.c_str()));
        inet_pton(AF_INET, Utf8(phone_ip_).c_str(), &addr.sin_addr);
        SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
        u_long nb = 1;
        ioctlsocket(s, FIONBIO, &nb);
        connect(s, (sockaddr*)&addr, sizeof(addr));
        fd_set w;
        FD_ZERO(&w);
        FD_SET(s, &w);
        timeval tv{4, 0};
        bool ok = select(0, nullptr, &w, nullptr, &tv) > 0;
        closesocket(s);
        if (status_) status_(ok ? L"Status: connected" : L"Status: link flap, monitoring...");
    }
}

bool TunnelManager::Start(const TunnelConfig& cfg, std::wstring* error) {
    if (up_.load()) {
        if (error) *error = L"already connected";
        return false;
    }
    if (log_) log_(L"precheck phone SOCKS5 " + cfg.phone_ip + L":" +
                   std::to_wstring(cfg.phone_port) + L"…");
    if (!PrecheckSocks5(cfg, error)) return false;
    phone_ip_ = cfg.phone_ip;
    phone_port_ = std::to_wstring(cfg.phone_port);
    std::wstring cfgPath;
    if (!WriteEngineConfig(cfg, &cfgPath)) {
        if (error) *error = L"failed to write engine config";
        return false;
    }
    if (log_) log_(L"starting lwIP engine (Job Object managed)...");
    if (!LaunchEngine(cfgPath, error)) return false;
    if (log_) log_(L"waiting for the TUN adapter...");
    if (!WaitAdapter(15, &conn_name_)) {
        if (error) *error = L"TUN adapter did not appear (engine may have exited)";
        Stop();
        return false;
    }
    ConfigureAdapter(conn_name_);
    if (log_) log_(L"adapter " + conn_name_ + L" configured; adding /1 routes...");
    if (!AddRoutes(conn_name_, error)) {
        Stop();
        return false;
    }
    up_.store(true);
    monitoring_.store(true);
    monitor_ = std::thread(&TunnelManager::MonitorLoop, this);
    if (log_) log_(L"tunnel established.");
    return true;
}

void TunnelManager::Stop() {
    monitoring_.store(false);
    up_.store(false);
    if (monitor_.joinable()) monitor_.join();
    if (engine_) {
        if (impl_->job) TerminateJobObject(impl_->job, 0);
        CloseHandle(engine_);
        engine_ = nullptr;
        engine_pid_ = 0;
    }
    CleanRoutes();
    Run(L"netsh interface set interface " + Q(kAdapterName) + L" disable");
    if (log_) log_(L"disconnected; routes cleaned.");
}

}  // namespace linkhelm
