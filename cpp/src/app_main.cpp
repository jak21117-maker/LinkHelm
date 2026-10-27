// LinkHelm Desktop — native C++ entry point (C++ rewrite P1/P2).
//
// Why this exists: the Tkinter build wedged on any blocking call (single UI
// thread + GIL) and lost its tray whenever the packaging venv lacked pystray.
// Here every network/probe operation runs on a worker thread and posts its
// result to the window; the tray is a plain Shell_NotifyIcon that cannot be
// "missing from the build".
//
// Scope of this first cut: the store-accelerator page (subscription import,
// node picker, latency test, auto-select) + tray residency. The Python build
// stays the fallback until every module has a C++ counterpart.
#define _WIN32_WINNT 0x0601
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <commdlg.h>
#include <shlobj.h>

#include <algorithm>
#include <cctype>
#include <deque>
#include <fstream>
#include <memory>
#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "accel_controller.h"
#include "file_transfer.h"
#include "ft_receiver.h"
#include "net_control.h"
#include "subscription.h"
#include "tray.h"
#include "tun_manager.h"

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "comdlg32.lib")
#pragma comment(lib, "shell32.lib")

namespace {

constexpr int kWidth = 620;
constexpr int kHeight = 860;

enum class Ctl : int {
    SubEdit = 101, PasteBtn, NodeCombo, DelayBtn, AutoBtn, RefreshBtn,
    ProxyUrlEdit, ProxyIpEdit, StartBtn, StopBtn, StatusLabel, LogBox,
    HostsLabel, HostsFixBtn,
    // beta1.0.85: connection page + transfer page (pure C++ shell)
    TabConn = 200, TabShare, TabAccel,
    PhoneIp, PhonePort, PhoneUser, PhonePass,
    ConnBtn, DiscBtn, ConnStatus,
    FtTarget, FtBrowse, FtSend, FtProgress, FtPairIp, FtPairBtn,
    UdpBtn, IcsBtn, SettingsBtn, AboutBtn, PassEye,
    FtDirBtn, FtRecvBtn, FtCancelBtn,
    TglAutostart = 30, TglAutoconnect = 31,
    IcsModeWifi, IcsModeEth, IcsEthCombo, IcsInfoLabel, IcsStartBtn, IcsStopBtn,
};

struct App {
    HWND hwnd = nullptr;
    HINSTANCE inst = nullptr;
    linkhelm::Tray* tray = nullptr;
    linkhelm::AccelController accel;
    std::unique_ptr<linkhelm::TunnelManager> tun;

    HWND sub_edit = nullptr, node_combo = nullptr, log_box = nullptr;
    HWND proxy_url = nullptr, proxy_ip = nullptr;
    HWND status = nullptr, start_btn = nullptr, stop_btn = nullptr;
    HWND hosts_label = nullptr, hosts_fix = nullptr;

    // tabs & pages
    HWND tab_conn = nullptr, tab_share = nullptr, tab_accel = nullptr;
    HWND page_conn = nullptr, page_share = nullptr, page_accel = nullptr;
    // connection page
    HWND phone_ip = nullptr, phone_port = nullptr, phone_user = nullptr,
        phone_pass = nullptr;
    HWND conn_btn = nullptr, disc_btn = nullptr, conn_status = nullptr;
    // transfer page
    HWND ft_target = nullptr, ft_browse = nullptr, ft_send = nullptr,
        ft_progress = nullptr, ft_pair_ip = nullptr, ft_pair_btn = nullptr;
    HWND ft_file = nullptr;
    HWND udp_btn = nullptr, ics_btn = nullptr;
    HWND settings_btn = nullptr, about_btn = nullptr;
    HWND ics_wifi = nullptr, ics_eth = nullptr, ics_eth_combo = nullptr;
    HWND ics_info = nullptr, ics_start = nullptr, ics_stop = nullptr;
    HWND ics_ssid = nullptr, ics_pass = nullptr;
    std::wstring pending_ssid, pending_pass;
    linkhelm::ReceiverServer* ft_rx = nullptr;
    std::wstring recv_dir;
    HWND pass_eye = nullptr;
    HWND ft_dir_label = nullptr, ft_dir_btn = nullptr;
    HWND ft_recv_btn = nullptr, ft_cancel_btn = nullptr, ft_local = nullptr;
    std::wstring ics_state;

    std::vector<std::wstring> node_names;
    std::wstring workdir;
    std::wstring mihomo_path;
    // directory holding geoip.dat / geosite.dat / Country.mmdb for the core
    std::wstring geo_dir;
    int mixed_port = 7897;
    int api_port = 9097;
    bool running = false;
    std::atomic<bool> ft_busy{false};
    // beta1.0.95: ICS start/stop double-click guard (racing PS scripts)
    std::atomic<bool> ics_busy{false};
    // beta1.0.96: delay-test double-click guard — two concurrent sweeps
    // double the WinHTTP load and starve the UI.
    std::atomic<bool> delay_busy{false};
    // beta1.0.98: start-boost double-click guard — repeated clicks used to
    // spawn several workers that fought over accel\config.yaml and the API
    // port (and doubled the subscription downloads).
    std::atomic<bool> start_busy{false};
    // beta1.0.97: group we select nodes into (always the built-in GLOBAL
    // selector in global mode; mihomo auto-adds every proxy to it).
    std::wstring accel_group = L"GLOBAL";
};

App g_app;

// beta1.0.85 i18n: default English (settings can switch to Chinese; applies
// to window titles and the main labels after a restart).
bool g_lang_zh = false;

// beta1.0.85 dark theme (user screenshot palette): deep charcoal background,
// blue accent, white text.
// beta1.0.86: the REAL LinkHelm palette (py 1.0.52 dark tuple)
constexpr COLORREF kBg = RGB(16, 24, 39);        // #101827
constexpr COLORREF kCard = RGB(27, 38, 56);      // #1B2638
constexpr COLORREF kText = RGB(240, 245, 252);   // #F0F5FC
constexpr COLORREF kMuted = RGB(169, 183, 201);  // #A9B7C9
constexpr COLORREF kAccent = RGB(0, 103, 192);   // #0067C0
constexpr COLORREF kAccentHover = RGB(0, 90, 171);  // #005AAB
constexpr COLORREF kBorder = RGB(52, 67, 89);    // #344359 card border
HBRUSH g_brBg = nullptr, g_brCard = nullptr;
const wchar_t* TR(const wchar_t* en, const wchar_t* zh) {
    return g_lang_zh ? zh : en;
}

std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n);
    return out;
}
std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), out.data(), n,
                        nullptr, nullptr);
    return out;
}

std::wstring GetText(HWND h) {
    int len = GetWindowTextLengthW(h);
    std::wstring out((size_t)len + 1, L'\0');
    GetWindowTextW(h, out.data(), len + 1);
    out.resize((size_t)len);
    return out;
}

// beta1.0.95 crash-hardening: worker threads call Log()/SetWindowTextW/
// EnableWindow on app windows. A destroyed (or half-destroyed) HWND makes
// those calls fail benignly on USER32, but cross-thread SendMessageW to a
// LISTBOX mid-destruction is a real crash source (window proc already gone).
// Guard with IsWindow + cap the log at 400 entries BEFORE adding.
// beta1.0.96 anti-freeze: Log() used to SendMessageW straight from worker
// threads — every log line blocked its worker on the UI pump, and during
// busy periods the pending cross-thread SendMessages serialized into
// visible UI lag ("首页卡顿"). Logs are now queued and drained by a single
// posted message, so workers never wait on the UI thread.
static std::mutex g_log_mtx;
static std::deque<std::wstring> g_log_q;
static FILE* g_log_file = nullptr;   // beta1.0.98: accel\linkhelm.log

// UTF-8 file sink: support can read exactly what the UI showed, and a
// crash mid-worker leaves the last step on disk instead of in a vanished
// window. Opened lazily on the first line, flushed every write.
static void LogToFileLocked(const std::wstring& line) {
    if (!g_log_file) {
        if (g_app.workdir.empty()) return;
        std::wstring p = g_app.workdir + L"\\linkhelm.log";
        g_log_file = _wfopen(p.c_str(), L"wb");
        if (!g_log_file) return;
    }
    std::string u = Utf8(line);
    fwrite(u.data(), 1, u.size(), g_log_file);
    fputc('\n', g_log_file);
    fflush(g_log_file);
}

void Log(const std::wstring& line) {
    {
        std::lock_guard<std::mutex> lk(g_log_mtx);
        g_log_q.push_back(line);
        LogToFileLocked(line);
    }
    if (g_app.hwnd && IsWindow(g_app.hwnd))
        PostMessageW(g_app.hwnd, WM_APP + 13, 0, 0);
}

// Runs on the UI thread only (WM_APP+13): drains the queue into the box.
void DrainLogQueue() {
    std::deque<std::wstring> batch;
    {
        std::lock_guard<std::mutex> lk(g_log_mtx);
        batch.swap(g_log_q);
    }
    if (batch.empty()) return;
    if (!g_app.log_box || !IsWindow(g_app.log_box)) return;
    for (const auto& line : batch) {
        int count = (int)SendMessageW(g_app.log_box, LB_GETCOUNT, 0, 0);
        while (count >= 400) {
            SendMessageW(g_app.log_box, LB_DELETESTRING, 0, 0);
            --count;
        }
        SendMessageW(g_app.log_box, LB_ADDSTRING, 0, (LPARAM)line.c_str());
    }
    int n = (int)SendMessageW(g_app.log_box, LB_GETCOUNT, 0, 0);
    if (n > 0) SendMessageW(g_app.log_box, LB_SETTOPINDEX, n - 1, 0);
}

// beta1.0.95: guarded UI helpers — every SetWindowTextW/EnableWindow from a
// worker thread goes through these, so a window destroyed mid-flight can
// never crash the app (field: "太容易崩").
void UiText(HWND h, const wchar_t* s) {
    if (h && IsWindow(h)) SetWindowTextW(h, s);
}
void UiEnable(HWND h, BOOL on) {
    if (h && IsWindow(h)) EnableWindow(h, on);
}

std::string WideToUtf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), out.data(), n,
                        nullptr, nullptr);
    return out;
}

void SetStatus(const std::wstring& text) {
    if (g_app.status) UiText(g_app.status, text.c_str());
}

int FreePort(int preferred) {
    for (int p = preferred; p < preferred + 200; ++p) {
        SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
        if (s == INVALID_SOCKET) continue;
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons((u_short)p);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        int ok = (bind(s, (sockaddr*)&addr, sizeof(addr)) == 0);
        closesocket(s);
        if (ok) return p;
    }
    return preferred;
}

// ---------------- helpers: mihomo-as-parser ----------------
// A subscription is written to a config and mihomo parses it natively; the
// node list is then read back from mihomo's REST API. This keeps ALL fragile
// YAML scanning out of the C++ process — a malformed subscription can no
// longer throw/abort and kill the UI (the root cause of "商店加速 干崩 UI").

static bool FileExistsW(const std::wstring& p) {
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES;
}

// ---------------- helpers: local proxy discovery ----------------
// beta1.0.99 fix: "download via 127.0.0.1:7897/7890 FAILED 12029" meant we
// guessed Clash mixed ports blindly. Real machines run v2rayN (10808/10809),
// Verge custom ports (33210...), or nothing at all. Probe what is actually
// LISTENING before trying it, and read the system proxy registry as well.

// Panel front-domain family: xfltd/cctvclient panels ship several hostnames
// for the SAME backend and token space. Individual fronts get DNS-poisoned
// (apikey.xfltd.click -> 8.7.198.46, times out direct); siblings stay
// reachable. Return the original URL first, then mirrors with the same
// scheme/path/query on the sibling hosts.
static std::vector<std::wstring> MirrorUrls(const std::wstring& url) {
    std::vector<std::wstring> out{url};
    size_t p = url.find(L"://");
    if (p == std::wstring::npos) return out;
    size_t hs = p + 3;
    size_t he = url.find_first_of(L"/?", hs);
    std::wstring host = url.substr(hs, he == std::wstring::npos
                                        ? std::wstring::npos : he - hs);
    std::wstring rest = he == std::wstring::npos ? L"" : url.substr(he);
    std::wstring scheme = url.substr(0, hs);
    // strip credentials/port noise: keep bare host for the family check
    auto endsWith = [](const std::wstring& h, const std::wstring& s) {
        return h.size() >= s.size() &&
               h.compare(h.size() - s.size(), s.size(), s) == 0;
    };
    std::vector<std::wstring> alts;
    if (endsWith(host, L".xfltd.click"))
        alts = {L"get.cctvclient.cn", L"api2.xfltd.cyou"};
    else if (endsWith(host, L".xfltd.cyou"))
        alts = {L"get.cctvclient.cn", L"apikey.xfltd.click"};
    else if (endsWith(host, L".cctvclient.cn"))
        alts = {L"api2.xfltd.cyou", L"apikey.xfltd.click"};
    for (const auto& a : alts) out.push_back(scheme + a + rest);
    return out;
}

// True when something accepts a TCP connection on 127.0.0.1:port (~300ms).
// A refused connect lands in the EXCEPT set on Windows — only a clean,
// SO_ERROR==0 writable socket counts as alive (no false positives).
static bool ProbeLocalPort(int port) {
    WSADATA w;
    bool init = WSAStartup(MAKEWORD(2, 2), &w) == 0;
    bool alive = false;
    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    if (s != INVALID_SOCKET) {
        u_long nb = 1;
        ioctlsocket(s, FIONBIO, &nb);  // non-blocking: connect + select
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons((u_short)port);
        a.sin_addr.S_un.S_addr = htonl(0x7F000001);  // 127.0.0.1
        connect(s, (sockaddr*)&a, sizeof(a));
        fd_set wset, eset;
        FD_ZERO(&wset);
        FD_ZERO(&eset);
        FD_SET(s, &wset);
        FD_SET(s, &eset);
        timeval tv{0, 300 * 1000};  // 300 ms
        if (select(0, nullptr, &wset, &eset, &tv) > 0 && FD_ISSET(s, &wset)) {
            int soerr = 0, sl = sizeof(soerr);
            getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&soerr, &sl);
            alive = (soerr == 0);
        }
        closesocket(s);
    }
    if (init) WSACleanup();
    return alive;
}

// System proxy from the registry (WinINET): "127.0.0.1:7890" or
// "http=127.0.0.1:7891;https=..." forms. Returns "" when disabled/absent.
static std::wstring GetSystemProxyServer() {
    HKEY k = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER,
                      L"Software\\Microsoft\\Windows\\CurrentVersion\\"
                      L"Internet Settings",
                      0, KEY_READ, &k) != ERROR_SUCCESS)
        return L"";
    DWORD enable = 0, szd = sizeof(enable);
    RegQueryValueExW(k, L"ProxyEnable", nullptr, nullptr,
                     (LPBYTE)&enable, &szd);
    wchar_t buf[256] = L"";
    DWORD szb = sizeof(buf) - sizeof(wchar_t);
    RegQueryValueExW(k, L"ProxyServer", nullptr, nullptr, (LPBYTE)buf, &szb);
    RegCloseKey(k);
    if (!enable || !buf[0]) return L"";
    std::wstring v = buf;
    // "http=...;https=..." -> prefer the https= entry, else http=
    size_t semi = v.find(L';');
    if (semi != std::wstring::npos) {
        std::wstring pick;
        size_t p = 0;
        while (p <= v.size()) {
            std::wstring item =
                v.substr(p, semi == std::wstring::npos
                                ? std::wstring::npos : semi - p);
            if (item.rfind(L"https=", 0) == 0) { pick = item.substr(6); break; }
            if (item.rfind(L"http=", 0) == 0 && pick.empty())
                pick = item.substr(5);
            if (semi == std::wstring::npos) break;
            p = semi + 1;
            semi = v.find(L';', p);
        }
        v = pick;
    }
    if (v.find(L':') == std::wstring::npos &&
        !v.empty())  // bare host -> default http proxy port
        v += L":80";
    return v;
}

// Local base64 sniff (the one in subscription.cpp is file-private, and this
// build has no exceptions so we never want to parse-and-throw on junk).
static bool LooksLikeBase64Str(const std::string& s) {
    if (s.empty()) return false;
    size_t n = 0;
    for (char c : s)
        if (std::isalnum((unsigned char)c) || c == '+' || c == '/' || c == '=')
            ++n;
    return n > s.size() * 9 / 10;
}

// Strip conflicting top-level (indent 0) keys from a clash/mihomo config and
// prepend our loopback-only profile. Pure line filtering — never throws.
static std::string BuildLoopbackConfig(const std::string& body,
                                       int mixed_port, int api_port) {
    static const char* kDrop[] = {
        "mixed-port", "allow-lan", "bind-address", "external-controller",
        "mode", "log-level", "secret", "external-ui", "find-process-mode",
        "global-client-fingerprint", "global-ua", "ipv6", "interface-name",
        "routing-mark", "tun", "experimental", "profile", "geodata-mode",
        "geox-url", "sub-rule", "script", "hosts", nullptr};
    std::string out =
        "# Generated by LinkHelm (C++ + mihomo parser) — loopback micro-kernel only.\n"
        "mixed-port: " + std::to_string(mixed_port) + "\n"
        "allow-lan: false\n"
        "bind-address: 127.0.0.1\n"
        "external-controller: 127.0.0.1:" + std::to_string(api_port) + "\n"
        "mode: global\n"
        "log-level: warning\n"
        "dns:\n"
        "  enable: false\n";
    size_t pos = 0;
    while (pos < body.size()) {
        size_t nl = body.find('\n', pos);
        std::string line = (nl == std::string::npos)
                               ? body.substr(pos)
                               : body.substr(pos, nl - pos);
        size_t sp = 0;
        while (sp < line.size() && (line[sp] == ' ' || line[sp] == '\t')) ++sp;
        if (sp == 0) {
            size_t colon = line.find(':');
            if (colon != std::string::npos) {
                std::string key = line.substr(0, colon);
                for (int k = 0; kDrop[k]; ++k)
                    if (key == kDrop[k]) {
                        pos = (nl == std::string::npos) ? body.size() : nl + 1;
                        goto next_line;
                    }
            }
        }
        out += line;
        out += '\n';
    next_line:
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    return out;
}

// Parse leaf proxy names out of mihomo's GET /proxies JSON. Group types
// (Selector/URLTest/Fallback/LoadBalance) and the built-ins (DIRECT/REJECT/
// GLOBAL/Pass/Compatible) are excluded. Single-pass, brace/bracket aware, no
// exceptions.
static std::vector<std::wstring> ParseLeafNodes(const std::string& json) {
    std::vector<std::wstring> out;
    size_t p = json.find("\"proxies\"");
    if (p == std::string::npos) return out;
    size_t i = json.find('{', p);
    if (i == std::string::npos) return out;
    int bd = 0;   // brace depth
    int kd = 0;   // bracket depth
    int pd = -1;  // brace depth of the "proxies" object
    bool inStr = false, esc = false;
    std::string tok;
    std::string prevKey;
    bool awaitValue = false;
    bool inEntry = false;
    std::wstring eName, eType; bool hN = false, hT = false;
    std::wstring skipTypes[] = {L"SELECTOR", L"URLTEST", L"FALLBACK",
                                L"LOADBALANCE", L"RELAY", L"SMART",
                                L"DIRECT", L"REJECT", L"REJECTDROP",
                                L"PASS", L"PASSRULE", L"COMPATIBLE"};
    auto finalize = [&]() {
        if (inEntry && hN && hT) {
            std::wstring u = eType;
            for (auto& c : u) c = (wchar_t)::towupper(c);
            bool isGroup = false;
            for (auto& t : skipTypes) if (u == t) { isGroup = true; break; }
            if (!isGroup) out.push_back(eName);
        }
        inEntry = false; hN = hT = false; eName.clear(); eType.clear();
    };
    for (size_t idx = i; idx < json.size(); ++idx) {
        char c = json[idx];
        if (inStr) {
            if (esc) { tok.push_back(c); esc = false; }
            else if (c == '\\') { tok.push_back(c); esc = true; }
            else if (c == '"') {
                inStr = false;
                bool atEntry = (bd == pd + 1 && kd == 0);
                if (atEntry) {
                    if (awaitValue) {
                        if (prevKey == "name") { eName = Utf16(tok); hN = true; }
                        else if (prevKey == "type") { eType = Utf16(tok); hT = true; }
                        awaitValue = false;
                    } else {
                        prevKey = tok;
                    }
                }
                tok.clear();
            } else tok.push_back(c);
            continue;
        }
        if (c == '"') { inStr = true; tok.clear(); continue; }
        if (c == '{') {
            ++bd; awaitValue = false;
            if (pd == -1) pd = bd;
            else if (bd == pd + 1) {
                inEntry = true; hN = hT = false;
                eName.clear(); eType.clear(); prevKey.clear();
            }
            continue;
        }
        if (c == '}') {
            if (inEntry && bd == pd + 1) finalize();
            --bd; continue;
        }
        if (c == '[') { ++kd; awaitValue = false; continue; }
        if (c == ']') { if (kd > 0) --kd; continue; }
        if (c == ':') { awaitValue = true; continue; }
        if (c == ',') { awaitValue = false; continue; }
    }
    return out;
}

// Turn a decoded share-link list into a mihomo config that loads it through a
// local proxy-provider — exactly how Clash Verge imports airport subscriptions.
static std::string BuildProviderConfig(int mixed_port, int api_port) {
    std::string s;
    s += "mixed-port: " + std::to_string(mixed_port) + "\n";
    s += "allow-lan: false\n";
    s += "bind-address: 127.0.0.1\n";
    s += "external-controller: 127.0.0.1:" + std::to_string(api_port) + "\n";
    s += "mode: global\n";
    s += "log-level: warning\n";
    s += "dns:/n  enable: false\n";
    s += "proxy-providers:/n  sub:/n    type: file\n    path: ./sub_links.txt\n";
    s += "    health-check:/n      enable: false\n";
    s += "proxy-groups:/n  - name: GLOBAL\n    type: select\n    use: [sub]\n";
    return s;
}

// Locate verge-mihomo.exe (Clash Verge Rev's Meta kernel). Newer bundled
// mihomo builds drop share-link parsing in proxy-providers, while the Clash
// Verge kernel still parses them — so this is the fallback parser.
static std::wstring FindVergeMihomo() {
    wchar_t exe[MAX_PATH]{};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe);
    size_t sl = dir.rfind(L'\\');
    if (sl != std::wstring::npos) dir = dir.substr(0, sl);
    std::wstring sib1 = dir + L"\\mihomo\\verge-mihomo.exe";
    if (FileExistsW(sib1)) return sib1;
    std::wstring sib2 = dir + L"\\verge-mihomo.exe";
    if (FileExistsW(sib2)) return sib2;
    // a running Clash Verge core (most reliable: the user already runs it)
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap != INVALID_HANDLE_VALUE) {
        PROCESSENTRY32W pe{};
        pe.dwSize = sizeof(pe);
        std::wstring found;
        if (Process32FirstW(snap, &pe)) {
            do {
                std::wstring nm = pe.szExeFile;
                if (nm != L"verge-mihomo.exe" && nm != L"clash-verge.exe") continue;
                HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                                       pe.th32ProcessID);
                if (!h) continue;
                wchar_t buf[MAX_PATH * 2]{};
                DWORD n = MAX_PATH * 2;
                if (QueryFullProcessImageNameW(h, 0, buf, &n)) {
                    std::wstring full(buf, n);
                    if (nm == L"verge-mihomo.exe") {
                        found = full;
                    } else {
                        std::wstring d = full;
                        size_t s2 = d.rfind(L'\\');
                        if (s2 != std::wstring::npos) {
                            d = d.substr(0, s2);
                            std::wstring v = d + L"\\verge-mihomo.exe";
                            if (FileExistsW(v)) found = v;
                        }
                    }
                }
                CloseHandle(h);
                if (!found.empty()) break;
            } while (Process32NextW(snap, &pe));
        }
        CloseHandle(snap);
        if (!found.empty()) return found;
    }
    // common install locations
    const wchar_t* fixed[] = {
        L"C://Program Files//Clash Verge//verge-mihomo.exe",
        L"C://Program Files (x86)//Clash Verge//verge-mihomo.exe",
        L"D://software//Clash Verge//verge-mihomo.exe", nullptr};
    for (int i = 0; fixed[i]; ++i)
        if (FileExistsW(fixed[i])) return fixed[i];
    wchar_t la[MAX_PATH]{};
    DWORD ln = GetEnvironmentVariableW(L"LOCALAPPDATA", la, MAX_PATH);
    if (ln > 0 && ln < MAX_PATH) {
        std::wstring pr = std::wstring(la) +
                          L"\\Programs\\Clash Verge\\verge-mihomo.exe";
        if (FileExistsW(pr)) return pr;
    }
    return L"";
}

// ---------------- worker: start (mihomo parses; verge-mihomo fallback) ----------------
void WorkerStart(std::wstring sub, std::wstring proxy) {
    auto fail = [&](const std::wstring& m) {
        g_app.accel.Stop();
        g_app.running = false;
        g_app.start_busy.store(false);
        SetStatus(m);
        UiEnable(g_app.start_btn, TRUE);
        UiEnable(g_app.stop_btn, FALSE);
    };

    // ---- 1) obtain subscription text: clash:// / URL / local file / pasted ----
    Log(L"[accel] start: input len " + std::to_wstring(sub.size()) +
        L", kind " + (sub.rfind(L"http", 0) == 0 ? L"url"
                    : (FileExistsW(sub) ? L"file" : L"text")) +
        L", head '" + sub.substr(0, 34) + L"', hwnd " +
        std::to_wstring((long long)(intptr_t)g_app.sub_edit));
    std::wstring inner;
    if (sub.rfind(L"clash://", 0) == 0 && linkhelm::UnwrapClashLink(sub, &inner)) {
        sub = inner;
        Log(L"[accel] clash:// one-click link unwrapped");
    }
    std::string body;
    std::wstring err;
    if (sub.rfind(L"http://", 0) == 0 || sub.rfind(L"https://", 0) == 0) {
        // beta1.0.99 fix: never guess blindly. Old chain hard-coded
        // 7897/7890 and 12029'd the moment the user's proxy used another
        // port, while DNS-poisoned fronts (apikey.xfltd.click) timed out
        // direct. Two dimensions now:
        //   URLs:   original -> sibling front-domains of the same panel
        //   proxy: system proxy (registry) -> user Proxy URL row ->
        //          locally LISTENING ports (300 ms probe) -> direct
        std::vector<std::wstring> proxies;
        std::wstring syspx = GetSystemProxyServer();
        if (!syspx.empty()) {
            proxies.push_back(L"http://" + syspx);
            Log(L"[accel] system proxy detected: " + syspx);
        }
        if (!proxy.empty()) proxies.push_back(proxy);
        static const int kPorts[] = {7897, 7890, 7891, 33210, 10809,
                                     10808, 2080, 1080, 8889, 8888, 20171};
        for (int port : kPorts) {
            std::wstring cand =
                L"http://127.0.0.1:" + std::to_wstring(port);
            bool dup = false;
            for (const auto& p : proxies)
                if (p == cand) { dup = true; break; }
            if (dup || !ProbeLocalPort(port)) continue;
            proxies.push_back(cand);
            Log(L"[accel] local proxy alive on port " +
                std::to_wstring(port));
        }
        proxies.push_back(L"");  // direct, bypasses any system proxy
        for (const auto& u : MirrorUrls(sub)) {
            if (u != sub) Log(L"[accel] trying sibling domain: " + u);
            for (const auto& px : proxies) {
                err.clear();
                body.clear();
                bool got = linkhelm::DownloadText(u, px,
                                                  px.empty() ? 9000 : 15000,
                                                  &body, &err);
                Log(L"[accel] download via " +
                    (px.empty() ? std::wstring(L"direct") : px) + L": " +
                    (got ? (std::to_wstring(body.size()) + L" bytes")
                         : (L"FAILED " + err)));
                if (got && !body.empty()) break;
                body.clear();
            }
            if (!body.empty()) break;
        }
        if (body.empty()) {
            fail(TR(L"Subscription download failed: ",
                    L"订阅下载失败：") + err +
                 TR(L" (panel domain needs a proxy - start your Clash/v2rayN "
                    L"or fill the Proxy URL row)",
                    L"（面板域名需走代理——请先开启 Clash/v2rayN，或填写 "
                    L"Proxy URL 一栏）"));
            Log(L"[accel] subscription download failed: " + err);
            return;
        }
    } else if ((sub.find(L'\\') != std::wstring::npos ||
                sub.find(L'/') != std::wstring::npos) &&
               FileExistsW(sub)) {
        std::ifstream fin(sub.c_str(), std::ios::binary);
        if (!fin) { fail(TR(L"Cannot read subscription file", L"无法读取订阅文件")); return; }
        body.assign((std::istreambuf_iterator<char>(fin)),
                    (std::istreambuf_iterator<char>()));
    } else {
        body = Utf8(sub);  // pasted raw text
    }
    if (body.empty()) { fail(TR(L"Subscription is empty", L"订阅内容为空")); return; }
    Log(L"[accel] payload " + std::to_wstring(body.size()) + L" bytes");
    // A panel that rejects the token (403) answers with an HTML error page.
    // Saying "unrecognised format" would send the user hunting the wrong bug.
    if (body.find("<!DOCTYPE") != std::string::npos ||
        body.find("<html") != std::string::npos) {
        fail(TR(L"Panel returned a web page (403 / token expired - re-copy "
                L"the subscription URL in your browser)",
                L"订阅返回的是网页（403/令牌失效，请在浏览器重新复制订阅地址）"));
        Log(L"[accel] panel returned an HTML page - 403 or expired token");
        return;
    }

    // ---- 2) normalise: base64 -> plain text (airports ship base64 share links) ----
    std::string content = body;
    if (LooksLikeBase64Str(content) &&
        content.find("proxies:") == std::string::npos &&
        content.find("://") == std::string::npos) {
        std::string dec;
        if (linkhelm::Base64Decode(content, &dec) && !dec.empty()) content = dec;
    }

    // ---- 3) classify: full Clash YAML vs bare share links ----
    bool hasProxies = content.find("proxies:") != std::string::npos ||
                      content.find("proxy-groups:") != std::string::npos;
    bool hasShare = content.find("vless://") != std::string::npos ||
                    content.find("vmess://") != std::string::npos ||
                    content.find("ss://") != std::string::npos ||
                    content.find("trojan://") != std::string::npos ||
                    content.find("hysteria2://") != std::string::npos ||
                    content.find("hy2://") != std::string::npos ||
                    content.find("tuic://") != std::string::npos;

    g_app.mixed_port = FreePort(7897);
    g_app.api_port = FreePort(9097);

    // beta1.0.98 (user directive): hand the subscription to the Clash-Meta
    // CORE and let IT parse — most airports only work with the modded core,
    // and the core is a single self-contained exe (no geo/MMDB dependency).
    // Two configs are prepared; each kernel gets the one it can actually use:
    //   cfg_provider : proxy-providers file -> share links (verge core parses)
    //   cfg_yaml     : proxies YAML converted in-process (bundled core)
    std::string cfg_yaml, cfg_provider;
    bool yaml_ok = false;
    if (hasProxies) {
        cfg_yaml = BuildLoopbackConfig(content, g_app.mixed_port, g_app.api_port);
        yaml_ok = true;
        Log(L"[accel] subscription is a Clash YAML config");
    } else if (hasShare) {
        std::wstring links_path = g_app.workdir + L"\\sub_links.txt";
        {
            std::ofstream lout(links_path.c_str(),
                               std::ios::binary | std::ios::trunc);
            if (!lout) { fail(TR(L"Cannot write node cache file", L"无法写入节点缓存文件")); return; }
            lout << content;
        }
        cfg_provider = BuildProviderConfig(g_app.mixed_port, g_app.api_port);
        std::string proxies_yaml;
        int converted = 0;
        linkhelm::BuildProxiesYamlFromLinks(content, &proxies_yaml, &converted);
        if (converted > 0) {
            cfg_yaml = BuildLoopbackConfig("proxies:\n" + proxies_yaml,
                                           g_app.mixed_port, g_app.api_port);
            yaml_ok = true;
        }
        Log(L"[accel] share-link list: " + std::to_wstring(converted) +
            L" link(s) convertible in-process; core provider config ready");
    } else {
        fail(TR(L"Subscription is neither a Clash config nor share links",
                L"订阅内容既不是 Clash 配置也不含节点链接"));
        Log(L"[accel] unrecognised subscription format");
        return;
    }

    std::wstring cfg_path = g_app.workdir + L"\\config.yaml";
    auto write_cfg = [&](const std::string& body) -> bool {
        std::ofstream fout(cfg_path.c_str(),
                           std::ios::binary | std::ios::trunc);
        if (!fout) return false;
        fout << body;
        return true;
    };

    // ---- 4) candidate kernels: modded verge core first, bundled second ----
    struct Cand {
        std::wstring path;
        std::wstring tag;
        bool provider;   // true: core parses the provider itself
    };
    std::vector<Cand> cands;
    std::wstring verge = FindVergeMihomo();
    if (!verge.empty() && !cfg_provider.empty())
        cands.push_back({verge, L"verge-mihomo", true});
    if (FileExistsW(g_app.mihomo_path)) {
        if (yaml_ok) cands.push_back({g_app.mihomo_path, L"mihomo", false});
        else if (!cfg_provider.empty())
            cands.push_back({g_app.mihomo_path, L"mihomo", true});
    }
    if (!verge.empty() && yaml_ok && verge != g_app.mihomo_path)
        cands.push_back({verge, L"verge-mihomo/yaml", false});
    if (cands.empty()) {
        fail(TR(L"mihomo kernel not found (mihomo.exe / verge-mihomo.exe)",
                L"未找到 mihomo 内核（mihomo.exe / verge-mihomo.exe）"));
        return;
    }

    std::vector<std::wstring> names;
    for (size_t ci = 0; ci < cands.size(); ++ci) {
        const std::string& cfg = cands[ci].provider ? cfg_provider : cfg_yaml;
        if (cfg.empty()) continue;
        if (!write_cfg(cfg)) { fail(TR(L"Cannot write config file", L"无法写入配置文件")); return; }
        SetStatus(TR(L"starting... (", L"启动中...（") + cands[ci].tag + L")");
        std::wstring error;
        if (!g_app.accel.Start(cands[ci].path, g_app.workdir, cfg_path,
                               g_app.mixed_port, g_app.api_port, &error)) {
            Log(L"[accel] " + cands[ci].tag + L" launch failed: " + error);
            continue;
        }
        g_app.running = true;
        names.clear();
        // A healthy core answers /proxies in ~1 s. Short probe windows keep
        // the fallback snappy (12 x 0.5 s) instead of hanging ~40 s per core.
        for (int attempt = 0; attempt < 12; ++attempt) {
            bool ok = false;
            std::wstring jw = g_app.accel.HttpGet(L"/proxies", 1000, &ok);
            if (ok) names = ParseLeafNodes(Utf8(jw));
            if (!names.empty()) break;
            Sleep(500);
        }
        if (!names.empty()) {
            Log(L"[accel] " + cands[ci].tag + L" parsed " +
                std::to_wstring(names.size()) + L" nodes");
            break;
        }
        Log(L"[accel] " + cands[ci].tag +
            L" parsed 0 nodes - trying next kernel");
        g_app.accel.Stop();
        g_app.running = false;
    }

    if (names.empty()) {
        // last-ditch local scan (only helps YAML-ish content)
        for (auto& n : linkhelm::ParseNodes(content))
            if (!n.name.empty()) names.push_back(n.name);
    }
    if (names.empty()) {
        fail(TR(L"No nodes parsed (tried mihomo and verge-mihomo)",
                L"订阅未解析到任何节点（已尝试 mihomo 与 verge-mihomo）"));
        Log(L"[accel] 0 nodes from all kernels + local fallback");
        return;
    }

    // user directive: keep a manageable slice for the UI list + latency sweep
    g_app.accel_group = L"GLOBAL";
    if (names.size() > 30) {
        Log(L"[accel] " + std::to_wstring(names.size()) +
            L" nodes parsed - keeping first 30 (UI cap)");
        names.resize(30);
    }
    g_app.node_names = names;
    PostMessageW(g_app.hwnd, WM_APP + 11, 0, 0);  // refresh node list
    SetStatus(TR(L"running - local proxy 127.0.0.1:", L"已运行 - 本地代理 127.0.0.1:") +
              std::to_wstring(g_app.mixed_port) + L" (" +
              std::to_wstring(names.size()) +
              TR(L" nodes)", L" 节点)"));
    UiEnable(g_app.stop_btn, TRUE);
    UiEnable(g_app.start_btn, FALSE);
    g_app.start_busy.store(false);
}


// ---------------- worker: latency test ----------------
void WorkerDelay(bool autoselect) {
    // beta1.0.96: double-click guard — a second sweep while one is running
    // doubles the WinHTTP load. Cleared on every exit path below.
    bool expected = false;
    if (!g_app.delay_busy.compare_exchange_strong(expected, true)) {
        SetStatus(TR(L"latency test already running", L"延迟测试正在进行"));
        return;
    }
    if (!g_app.running) {
        SetStatus(TR(L"start the accelerator first", L"请先启动商店加速"));
        g_app.delay_busy.store(false);
        return;
    }
    if (g_app.node_names.empty()) {
        SetStatus(TR(L"no nodes to test", L"没有可测试的节点"));
        g_app.delay_busy.store(false);
        return;
    }
    std::vector<linkhelm::DelayResult> results =
        // beta1.0.98 (user directive): GitHub as the test site, and a node
        // only counts as timed out after the full 5000 ms — the HTTP-layer
        // margin in TestDelay must exceed that so the API answer isn't cut
        // short before the node had its full window.
        g_app.accel.TestAllDelays(g_app.node_names, 5000, L"https://github.com");
    std::wstring best;
    long long best_ms = -1;
    for (const auto& r : results) {
        if (r.ms >= 0) {
            Log(L"[delay] " + r.node + L": " + std::to_wstring(r.ms) + L" ms");
            if (best_ms < 0 || r.ms < best_ms) { best = r.node; best_ms = r.ms; }
        } else {
            Log(L"[delay] " + r.node + L": timeout");
        }
    }
    if (autoselect && !best.empty()) {
        std::wstring group = L"GLOBAL";
        std::string g = linkhelm::FirstSelectableGroup("");
        if (!g.empty()) group = Utf16(g);
        if (g_app.accel.SelectNode(group, best)) {
            SetStatus(TR(L"auto-selected ", L"已自动选择 ") + best + L" (" +
            std::to_wstring(best_ms) + L" ms)");
        }
    } else if (!best.empty()) {
        SetStatus(TR(L"fastest: ", L"最快节点: ") + best + L" (" +
            std::to_wstring(best_ms) + L" ms)");
    } else {
        SetStatus(TR(L"no node answered the latency test", L"没有节点通过延迟测试"));
    }
    g_app.delay_busy.store(false);
    PostMessageW(g_app.hwnd, WM_APP + 12, 0, 0);  // repaint badges
}

// ---------------- UI ----------------
void OnStart(HWND) {
    std::wstring sub = GetText(g_app.sub_edit);
    if (sub.empty()) {
        SetStatus(TR(L"paste a subscription link first", L"请先粘贴订阅链接"));
        return;
    }
    // beta1.0.98: one worker at a time (double-click used to spawn several
    // workers racing on config.yaml + the API port).
    bool expected = false;
    if (!g_app.start_busy.compare_exchange_strong(expected, true)) {
        SetStatus(TR(L"already starting...", L"正在启动，请稍候..."));
        return;
    }
    std::wstring proxy = GetText(g_app.proxy_url);
    // beta1.0.99: no more Proxy IP + ":7897" guessing. The port-probe chain
    // in WorkerStart covers every common local proxy port (and the system
    // proxy), so a bare IP with an invented port only adds a guaranteed
    // failure to the candidate list. The Proxy IP row stays for the actual
    // tunneled traffic (unchanged behaviour elsewhere).
    UiEnable(g_app.start_btn, FALSE);
    SetStatus(TR(L"starting... (download subscription, launch kernel)",
                L"启动中...（下载订阅并拉起内核）"));
    std::thread(WorkerStart, sub, proxy).detach();
}

void OnStop(HWND) {
    g_app.accel.Stop();
    g_app.running = false;
    g_app.start_busy.store(false);   // allow a fresh start after a cancel
    UiEnable(g_app.start_btn, TRUE);
    UiEnable(g_app.stop_btn, FALSE);
    SetStatus(TR(L"stopped", L"已停止"));
}

// beta1.0.99: airports name their nodes in Chinese ("🇭🇰 香港 01 [V]").
// In English mode translate the well-known region/service words for DISPLAY
// only — node selection always goes through the index into node_names, so
// the real name still reaches the core API untouched.
std::wstring NodeDisplayName(const std::wstring& n) {
    if (g_lang_zh) return n;
    static const std::pair<const wchar_t*, const wchar_t*> map[] = {
        {L"香港", L"Hong Kong"}, {L"台湾", L"Taiwan"}, {L"澳门", L"Macao"},
        {L"日本", L"Japan"},     {L"韩国", L"Korea"},  {L"新加坡", L"Singapore"},
        {L"美国", L"USA"},       {L"加拿大", L"Canada"},{L"英国", L"UK"},
        {L"德国", L"Germany"},   {L"法国", L"France"}, {L"荷兰", L"Netherlands"},
        {L"俄罗斯", L"Russia"},  {L"澳大利亚", L"Australia"}, {L"马来西亚", L"Malaysia"},
        {L"泰国", L"Thailand"},  {L"越南", L"Vietnam"},{L"菲律宾", L"Philippines"},
        {L"印度", L"India"},     {L"巴西", L"Brazil"}, {L"阿根廷", L"Argentina"},
        {L"土耳其", L"Turkey"},  {L"意大利", L"Italy"},{L"西班牙", L"Spain"},
        {L"瑞士", L"Switzerland"},{L"瑞典", L"Sweden"},
        {L"印尼", L"Indonesia"}, {L"柬埔寨", L"Cambodia"},{L"老挝", L"Laos"},
        {L"阿联酋", L"UAE"},     {L"沙特", L"Saudi"},
        {L"官网", L"Official"},  {L"主页", L"Home"},
        {L"专线", L"Premium"},   {L"中转", L"Relay"},  {L"家宽", L"Home BB"},
        {L"残留", L"Residual"},  {L"实验", L"Experimental"},
        {L"倍率", L"Rate"},      {L"测试", L"Test"},
    };
    std::wstring out = n;
    for (auto& kv : map) {
        size_t pos = 0;
        while ((pos = out.find(kv.first, pos)) != std::wstring::npos) {
            out.replace(pos, wcslen(kv.first), kv.second);
            pos += wcslen(kv.second);
        }
    }
    return out;
}

void RefreshNodes() {
    if (!g_app.node_combo) return;
    SendMessageW(g_app.node_combo, CB_RESETCONTENT, 0, 0);
    for (const auto& n : g_app.node_names) {
        std::wstring disp = NodeDisplayName(n);
        SendMessageW(g_app.node_combo, CB_ADDSTRING, 0,
                     (LPARAM)disp.c_str());
    }
    if (!g_app.node_names.empty()) {
        SendMessageW(g_app.node_combo, CB_SETCURSEL, 0, 0);
    }
}

HWND MakeButton(HWND parent, const wchar_t* text, int x, int y, int w, int h,
                int id) {
    return CreateWindowW(L"BUTTON", text, WS_TABSTOP | WS_VISIBLE | WS_CHILD,
                         x, y, w, h, parent, (HMENU)(INT_PTR)id, g_app.inst, nullptr);
}

HWND MakeLabel(HWND parent, const wchar_t* text, int x, int y, int w, int h) {
    HWND label = CreateWindowW(L"STATIC", text, WS_VISIBLE | WS_CHILD, x, y,
                               w, h, parent, nullptr, g_app.inst, nullptr);
    // beta1.0.87: bigger, less cramped (user feedback)
    static HFONT f = CreateFontW(17, 0, 0, 0, FW_NORMAL, 0, 0, 0,
                                 DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                                 DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    SendMessageW(label, WM_SETFONT, (WPARAM)f, TRUE);
    return label;
}


// ============ beta1.0.86: CTk-style dark widgets (owner-draw) ============
constexpr COLORREF kInput = RGB(32, 45, 64);     // #202D40 input bg
constexpr COLORREF kBtnFill = RGB(42, 59, 81);   // #2A3B51 secondary
HBRUSH g_brInput = nullptr;

static bool IsAccentBtn(HWND h) { return GetPropW(h, L"lhAccent") != nullptr; }
static void MarkAccent(HWND h) { SetPropW(h, L"lhAccent", (HANDLE)1); }

// card rects per page (registered at build; painted in WM_PAINT)
struct CardRc { HWND page; RECT rc; };
static std::vector<CardRc> g_cards;
static void AddCard(HWND page, int x, int y, int w, int h) {
    CardRc c;
    c.page = page;
    c.rc = {x, y, x + w, y + h};
    g_cards.push_back(c);
}
static void PaintCards(HWND h) {
    PAINTSTRUCT ps;
    HDC dc = GetDC(h);
    RECT full;
    GetClientRect(h, &full);
    for (auto& c : g_cards) {
        if (c.page != h) continue;
        HRGN rg = CreateRoundRectRgn(c.rc.left, c.rc.top, c.rc.right + 1,
                                     c.rc.bottom + 1, 10, 10);
        HBRUSH br = CreateSolidBrush(kCard);
        FillRgn(dc, rg, br);
        DeleteObject(br);
        HBRUSH hb = CreateSolidBrush(kBorder);
        FrameRgn(dc, rg, hb, 1, 1);
        DeleteObject(hb);
        DeleteObject(rg);
    }
    ReleaseDC(h, dc);
}

// one painter for owner-draw buttons / radio pills / checkboxes / combo items
static bool DrawDarkCtl(DRAWITEMSTRUCT* d) {
    if (!d) return false;
    HDC dc = d->hDC;
    RECT r = d->rcItem;
    if (d->CtlType == ODT_COMBOBOX) {
        if (d->itemID == (UINT)-1) return true;
        wchar_t txt[180];
        int n = (int)SendMessageW(d->hwndItem, CB_GETLBTEXT, d->itemID,
                                  (LPARAM)txt);
        bool sel = (d->itemState & ODS_SELECTED) != 0;
        HBRUSH br = CreateSolidBrush(sel ? kAccent : kInput);
        FillRect(dc, &r, br);
        DeleteObject(br);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(255, 255, 255));
        RECT tr = r;
        tr.left += 10;
        DrawTextW(dc, txt, n, &tr, DT_LEFT | DT_VCENTER | DT_SINGLELINE |
                                     DT_END_ELLIPSIS);
        return true;
    }
    if (d->CtlType != ODT_BUTTON) return false;
    wchar_t txt[80];
    GetWindowTextW(d->hwndItem, txt, 80);
    LONG st = GetWindowLongW(d->hwndItem, GWL_STYLE);
    bool radio = (st & BS_AUTORADIOBUTTON) != 0;
    bool check = (st & BS_AUTOCHECKBOX) != 0;
    bool dis = (d->itemState & ODS_DISABLED) != 0;
    bool chk = (d->itemState & ODS_CHECKED) != 0;
    bool prs = (d->itemState & ODS_SELECTED) != 0;
    bool acc = IsAccentBtn(d->hwndItem);
    SetBkMode(dc, TRANSPARENT);

    if (radio || check) {
        // pill / checkbox on the page background
        if (radio) {
            int rad = 12;
            HBRUSH br = CreateSolidBrush(chk ? kAccent : kBtnFill);
            HRGN rg = CreateRoundRectRgn(r.left, r.top, r.right + 1,
                                         r.bottom + 1, rad, rad);
            FillRgn(dc, rg, br);
            DeleteObject(rg);
            DeleteObject(br);
            // dot
            int cy = (r.top + r.bottom) / 2, dd = 8;
            HBRUSH wb = CreateSolidBrush(
                chk ? RGB(255, 255, 255) : kMuted);
            HGDIOBJ ob = SelectObject(dc, wb);
            HGDIOBJ op = SelectObject(dc, GetStockObject(NULL_PEN));
            Ellipse(dc, 14, cy - dd / 2, 14 + dd, cy + dd / 2);
            SelectObject(dc, op);
            SelectObject(dc, ob);
            DeleteObject(wb);
        } else {
            // beta1.0.91: CTk-style TOGGLE SWITCH (pill + sliding knob)
            int cy = (r.top + r.bottom) / 2;
            RECT trk{r.left + 4, cy - 9, r.left + 40, cy + 9};
            HBRUSH trkBr = CreateSolidBrush(chk ? kAccent : RGB(70, 70, 78));
            HRGN pill = CreateRoundRectRgn(trk.left, trk.top, trk.right + 1,
                                           trk.bottom + 1, 18, 18);
            FillRgn(dc, pill, trkBr);
            DeleteObject(trkBr);
            DeleteObject(pill);
            // knob
            int kd = 14;
            int kx = chk ? trk.right - kd - 2 : trk.left + 2;
            HBRUSH kb = CreateSolidBrush(RGB(255, 255, 255));
            HGDIOBJ ok = SelectObject(dc, kb);
            HGDIOBJ op = SelectObject(dc, GetStockObject(NULL_PEN));
            Ellipse(dc, kx, cy - kd / 2, kx + kd, cy + kd / 2);
            SelectObject(dc, op);
            SelectObject(dc, ok);
            DeleteObject(kb);
        }
        SetTextColor(dc, dis ? kMuted : kText);
        RECT tr = r;
        tr.left = radio || check ? 30 : r.left;
        DrawTextW(dc, txt, -1, &tr,
                  DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        return true;
    }
    // eye toggle: draw accent-tinted so the state is visible
    if (GetPropW(d->hwndItem, L"lhEye")) {
        LONG pst = GetWindowLongW(g_app.phone_pass, GWL_STYLE);
        bool hidden = (pst & ES_PASSWORD) != 0;
        COLORREF ef = hidden ? kBtnFill : kAccent;
        HBRUSH eb = CreateSolidBrush(ef);
        HRGN erg = CreateRoundRectRgn(r.left, r.top, r.right + 1,
                                      r.bottom + 1, 8, 8);
        FillRgn(dc, erg, eb);
        DeleteObject(erg);
        DeleteObject(eb);
        SetTextColor(dc, RGB(255, 255, 255));
        DrawTextW(dc, txt, -1, &r, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        return true;
    }
    // standard rounded button
    bool focus = (d->itemState & ODS_FOCUS) != 0;
    COLORREF fill = acc ? (prs ? kAccentHover : (focus ? kAccentHover : kAccent))
                        : (prs ? RGB(52, 67, 89)
                               : (focus ? RGB(52, 72, 99) : kBtnFill));
    HBRUSH br = CreateSolidBrush(fill);
    HRGN rg = CreateRoundRectRgn(r.left, r.top, r.right + 1, r.bottom + 1,
                                 8, 8);
    FillRgn(dc, rg, br);
    DeleteObject(rg);
    DeleteObject(br);
    // beta1.0.95: disabled = obviously darker fill + muted italic text so
    // users can SEE which actions are spent (original-app behaviour).
    if (dis) {
        HBRUSH db = CreateSolidBrush(RGB(28, 38, 54));  // clearly darker
        FillRgn(dc, rg, db);
        DeleteObject(db);
        SetTextColor(dc, RGB(110, 122, 138));
        // italic to differentiate from enabled
        HFONT df = CreateFontW(16, 0, 0, 0, FW_NORMAL, TRUE, 0, 0,
                               DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                               DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        HGDIOBJ of2 = SelectObject(dc, df);
        DrawTextW(dc, txt, -1, &r,
                  DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        SelectObject(dc, of2);
        DeleteObject(df);
        DeleteObject(rg);
        return true;
    }
    SetTextColor(dc, RGB(255, 255, 255));
    DrawTextW(dc, txt, -1, &r,
              DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
    return true;
}

// pages are the DIRECT parent of every field -> color/draw messages land HERE
static void PaintCards(HWND h);

static LRESULT CALLBACK PageProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(h, &ps);
            PaintCards(h);
            EndPaint(h, &ps);
            return 0;
        }
        case WM_ERASEBKGND: {
            RECT rc;
            GetClientRect(h, &rc);
            FillRect((HDC)w, &rc, g_brBg);
            return TRUE;
        }
        case WM_COMMAND:
            // beta1.0.86 fix: children of the page (all cards' buttons,
            // combos, edits) notify THIS window; forward to the main window
            // so WndProc's handlers actually run. Without this every button
            // inside a page was dead.
            SendMessageW(GetParent(h), WM_COMMAND, w, l);
            return 0;
        case WM_CTLCOLORBTN: {
            // plain radios/checkboxes: transparent-ish dark fill
            HDC dc = (HDC)w;
            SetTextColor(dc, kText);
            SetBkColor(dc, kBg);
            return (LRESULT)g_brBg;
        }
        case WM_CTLCOLORSTATIC: {
            HDC dc = (HDC)w;
            SetTextColor(dc, kText);
            SetBkColor(dc, kBg);
            return (LRESULT)g_brBg;
        }
        case WM_CTLCOLOREDIT:
        case WM_CTLCOLORLISTBOX: {
            HDC dc = (HDC)w;
            SetTextColor(dc, RGB(255, 255, 255));
            SetBkColor(dc, kInput);
            return (LRESULT)g_brInput;
        }
        case WM_DRAWITEM:
            if (DrawDarkCtl((DRAWITEMSTRUCT*)l)) return TRUE;
            break;
        case WM_MEASUREITEM: {
            MEASUREITEMSTRUCT* mi = (MEASUREITEMSTRUCT*)l;
            if (mi->CtlType == ODT_COMBOBOX) {
                mi->itemHeight = 20;
                return TRUE;
            }
            break;
        }
    }
    return DefWindowProcW(h, m, w, l);
}

std::wstring RunPowerShell(const std::wstring& script, DWORD timeoutMs = 15000);

void BuildUi(HWND hwnd) {
    // ---- tab strip (pages mirror the py app: 连接 / 共享传输 / 加速) ----
    g_app.tab_conn = CreateWindowW(L"BUTTON", TR(L"Connect", L"连接"),
        WS_VISIBLE | WS_CHILD | BS_AUTORADIOBUTTON | BS_OWNERDRAW | WS_GROUP,
        12, 8, 92, 26, hwnd, (HMENU)(INT_PTR)Ctl::TabConn, g_app.inst, nullptr);
    g_app.tab_share = CreateWindowW(L"BUTTON", TR(L"Share/Transfer", L"共享传输"),
        WS_VISIBLE | WS_CHILD | BS_AUTORADIOBUTTON | BS_OWNERDRAW,
        112, 8, 150, 26, hwnd, (HMENU)(INT_PTR)Ctl::TabShare, g_app.inst, nullptr);
    g_app.tab_accel = CreateWindowW(L"BUTTON", TR(L"Store Boost", L"商店加速"),
        WS_VISIBLE | WS_CHILD | BS_AUTORADIOBUTTON | BS_OWNERDRAW,
        270, 8, 130, 26, hwnd, (HMENU)(INT_PTR)Ctl::TabAccel, g_app.inst, nullptr);
    SendMessageW(g_app.tab_conn, BM_SETCHECK, BST_CHECKED, 0);
    g_app.settings_btn = CreateWindowW(L"BUTTON",
        TR(L"Settings", L"设置"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        kWidth - 104, 8, 88, 26, hwnd,
        (HMENU)(INT_PTR)Ctl::SettingsBtn, g_app.inst, nullptr);
    g_app.about_btn = CreateWindowW(L"BUTTON",
        TR(L"About", L"声明"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        kWidth - 204, 8, 92, 26, hwnd,
        (HMENU)(INT_PTR)Ctl::AboutBtn, g_app.inst, nullptr);

    // ---- page containers ----
    g_app.page_conn = CreateWindowW(L"LhPage", L"",
        WS_CHILD | WS_VISIBLE, 0, 40, kWidth, kHeight - 40, hwnd, nullptr,
        g_app.inst, nullptr);
    // beta1.0.9 ROOT FIX: share/accel pages must NOT be visible at build —
    // all three stacked on top of each other made every page look broken.
    g_app.page_share = CreateWindowW(L"LhPage", L"",
        WS_CHILD, 0, 40, kWidth, kHeight - 40, hwnd, nullptr,
        g_app.inst, nullptr);
    g_app.page_accel = CreateWindowW(L"LhPage", L"",
        WS_CHILD, 0, 40, kWidth, kHeight - 40, hwnd, nullptr,
        g_app.inst, nullptr);

    // page header (py style: big bold title + client badge)
    HWND pc = g_app.page_conn;
    HWND title = CreateWindowW(L"STATIC",
        TR(L"Connect & Share", L"连接与共享"),
        WS_CHILD | WS_VISIBLE, 16, 6, 300, 30, pc, nullptr, g_app.inst,
        nullptr);
    // bigger bold font for the header
    {
        HFONT f = CreateFontW(24, 0, 0, 0, FW_BOLD, 0, 0, 0,
                              DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                              DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        SendMessageW(title, WM_SETFONT, (WPARAM)f, TRUE);
    }
    HWND badge = CreateWindowW(L"STATIC", L"WINDOWS CLIENT",
        WS_CHILD | WS_VISIBLE, 470, 10, 120, 20, pc, nullptr, g_app.inst,
        nullptr);
    {
        HFONT f = CreateFontW(13, 0, 0, 0, FW_BOLD, 0, 0, 0,
                              DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                              DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        SendMessageW(badge, WM_SETFONT, (WPARAM)f, TRUE);
    }
    int y = 76;  // beta1.0.89: below the 24pt header + badge (was 44 -> stacking)
    // card background painted by PageProc (no child window -> cannot
    // intercept clicks or cover the controls - the 1.0.87 dead-button fix)
    AddCard(pc, 12, y, kWidth - 40, 150);
    {
        HWND h = MakeLabel(pc, TR(L"01  INBOUND", L"01  入站连接"),
                           26, y + 10, 200, 18);
        HFONT f = CreateFontW(15, 0, 0, 0, FW_BOLD, 0, 0, 0,
                              DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                              DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        SendMessageW(h, WM_SETFONT, (WPARAM)f, TRUE);
    }
    y += 34;
    MakeLabel(pc, TR(L"Phone IP", L"手机IP"), 26, y + 4, 62, 20);
    g_app.phone_ip = CreateWindowW(L"EDIT", L"192.168.49.1",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL, 96, y, 140, 24, pc, nullptr,
        g_app.inst, nullptr);
    MakeLabel(pc, TR(L"Port", L"端口"), 246, y + 4, 40, 20);
    g_app.phone_port = CreateWindowW(L"EDIT", L"8080",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL, 292, y, 60, 24, pc, nullptr,
        g_app.inst, nullptr);
    MakeLabel(pc, TR(L"User", L"账号"), 362, y + 4, 40, 20);
    g_app.phone_user = CreateWindowW(L"EDIT", L"linkhelm01",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL, 408, y, 90, 24, pc, nullptr,
        g_app.inst, nullptr);
    y += 32;
    MakeLabel(pc, TR(L"Password", L"密码"), 26, y + 4, 62, 20);
    g_app.phone_pass = CreateWindowW(L"EDIT", L"k7h2m9x4qa",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL | ES_PASSWORD, 96, y, 140, 24,
        pc, nullptr, g_app.inst, nullptr);
    g_app.pass_eye = CreateWindowW(L"BUTTON", L"\U0001F441",
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        244, y - 3, 46, 28, pc, (HMENU)(INT_PTR)Ctl::PassEye,
        g_app.inst, nullptr);
    SetPropW(g_app.pass_eye, L"lhEye", (HANDLE)1);
    g_app.udp_btn = CreateWindowW(L"BUTTON", TR(L"Check UDP", L"检测UDP"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        480, y - 1, 110, 26, pc, (HMENU)(INT_PTR)Ctl::UdpBtn,
        g_app.inst, nullptr);
    y += 36;
    g_app.conn_btn = CreateWindowW(L"BUTTON", TR(L"Connect", L"一键连接"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        26, y, 270, 32, pc, (HMENU)(INT_PTR)Ctl::ConnBtn,
        g_app.inst, nullptr);
    MarkAccent(g_app.conn_btn);
    g_app.disc_btn = CreateWindowW(L"BUTTON", TR(L"Disconnect", L"断开"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        306, y, 270, 32, pc, (HMENU)(INT_PTR)Ctl::DiscBtn,
        g_app.inst, nullptr);
    UiEnable(g_app.disc_btn, FALSE);
    y += 44;

    // ===== 02 SHARING (ICS card) =====
    AddCard(pc, 12, y, kWidth - 40, 148);
    {
        HWND h = MakeLabel(pc, TR(L"02  OUTBOUND", L"02  出站分发"),
                           26, y + 10, 200, 18);
        HFONT f = CreateFontW(15, 0, 0, 0, FW_BOLD, 0, 0, 0,
                              DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                              DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        SendMessageW(h, WM_SETFONT, (WPARAM)f, TRUE);
    }
    y += 34;
    // beta1.0.95 ROOT FIX: BS_AUTORADIOBUTTON + BS_OWNERDRAW is unsupported
    // (the radio state machine cannot round-trip through owner-draw — clicks
    // never stuck). Plain radios + dark WM_CTLCOLOR keep the theme; the pill
    // look is gone but the MODE SWITCHES, which is what matters.
    g_app.ics_wifi = CreateWindowW(L"BUTTON",
        TR(L"Wi-Fi Hotspot", L"Wi-Fi 热点"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_AUTORADIOBUTTON |
            WS_GROUP, 26, y, 140, 24, pc,
        (HMENU)(INT_PTR)Ctl::IcsModeWifi, g_app.inst, nullptr);
    g_app.ics_eth = CreateWindowW(L"BUTTON", TR(L"Ethernet", L"以太网"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_AUTORADIOBUTTON,
        174, y, 100, 24, pc, (HMENU)(INT_PTR)Ctl::IcsModeEth, g_app.inst,
        nullptr);
    g_app.ics_eth_combo = CreateWindowW(L"COMBOBOX", L"",
        WS_VISIBLE | WS_CHILD | CBS_DROPDOWNLIST | CBS_OWNERDRAWFIXED |
            CBS_HASSTRINGS | WS_VSCROLL, 282, y, 274, 200, pc,
        (HMENU)(INT_PTR)Ctl::IcsEthCombo, g_app.inst, nullptr);
    SendMessageW(g_app.ics_wifi, BM_SETCHECK, BST_CHECKED, 0);
    y += 30;
    // hotspot credentials line: prefilled with the CURRENT system hotspot
    // config on open; refreshed again after Start Sharing.
    MakeLabel(pc, TR(L"SSID", L"热点名称"), 26, y + 4, 56, 20);
    g_app.ics_ssid = CreateWindowW(L"EDIT", L"",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL | ES_READONLY,
        76, y, 150, 24, pc, nullptr, g_app.inst, nullptr);
    MakeLabel(pc, TR(L"passWord", L"热点密码"), 236, y + 4, 70, 20);
    g_app.ics_pass = CreateWindowW(L"EDIT", L"",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL | ES_READONLY,
        296, y, 150, 24, pc, nullptr, g_app.inst, nullptr);

    // beta1.0.91: ASYNC hotspot SSID/pass prefill — the sync version froze
    // WM_CREATE for up to 15s (UI looked dead, fields empty on quick looks).
    std::thread([] {
        std::wstring rep = RunPowerShell(
            L"$ErrorActionPreference='SilentlyContinue'\n"
            L"$p=[Windows.Networking.Connectivity.NetworkInformation,"
            L"Windows.Networking.Connectivity,ContentType=WindowsRuntime]"
            L"::GetInternetConnectionProfile()\n"
            L"if($p){$m=[Windows.Networking.NetworkOperators."
            L"NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,"
            L"ContentType=WindowsRuntime]::CreateFromConnectionProfile($p)\n"
            L"$c=$m.GetCurrentAccessPointConfiguration()\n"
            L"Write-Output ('SSID:'+$c.Ssid)\n"
            L"Write-Output ('PASS:'+$c.Passphrase)}\n");
        auto pick = [](const std::wstring& rep, const wchar_t* tag) {
            std::wstring v;
            size_t p1 = rep.find(tag);
            if (p1 != std::wstring::npos) {
                size_t e = rep.find(L'\n', p1);
                v = rep.substr(p1 + wcslen(tag),
                               e == std::wstring::npos
                                   ? std::wstring::npos
                                   : e - p1 - wcslen(tag));
                while (!v.empty() && (v.back() == L'\r' || v.back() == L' '))
                    v.pop_back();
            }
            return v;
        };
        std::wstring ssid0 = pick(rep, L"SSID:");
        std::wstring pass0 = pick(rep, L"PASS:");
        if (g_app.ics_ssid && !ssid0.empty())
            UiText(g_app.ics_ssid, ssid0.c_str());
        if (g_app.ics_pass && !pass0.empty())
            UiText(g_app.ics_pass, pass0.c_str());
        if (!ssid0.empty()) Log(L"[ics] hotspot: " + ssid0);
    }).detach();
    y += 30;
    g_app.ics_info = MakeLabel(pc, TR(L"Sharing off", L"未开启共享"), 26, y,
                               400, 20);
    y += 26;
    g_app.ics_start = CreateWindowW(L"BUTTON",
        TR(L"Start Sharing", L"开始共享"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        26, y, 270, 30, pc, (HMENU)(INT_PTR)Ctl::IcsStartBtn,
        g_app.inst, nullptr);
    MarkAccent(g_app.ics_start);
    g_app.ics_stop = CreateWindowW(L"BUTTON",
        TR(L"Stop Sharing", L"停止共享"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW | WS_DISABLED,
        306, y, 270, 30, pc, (HMENU)(INT_PTR)Ctl::IcsStopBtn, g_app.inst,
        nullptr);
    y += 40;

    g_app.conn_status = MakeLabel(pc, TR(L"Status: not connected", L"状态：未连接"),
                                  16, y, 540, 20);
    y += 24;
    g_app.log_box = CreateWindowW(L"LISTBOX", L"",
        WS_VISIBLE | WS_CHILD | WS_VSCROLL | LBS_NOTIFY |
            LBS_NOINTEGRALHEIGHT,
        16, y, 568, 250, pc, (HMENU)(INT_PTR)Ctl::LogBox, g_app.inst,
        nullptr);

    // ===== Transfer page (py 1.0.84 layout) =====
    HWND ps = g_app.page_share;
    int ty = 10;
    MakeLabel(ps, TR(L"File Transfer", L"文件传输"), 16, ty, 200, 24);
    ty += 30;
    // local address (what the peer should pair TO) - live detection
    MakeLabel(ps, TR(L"This device (enter on the peer):",
                     L"本机地址（填到对方）："), 16, ty, 300, 20);
    g_app.ft_local = MakeLabel(ps, L"...", 320, ty, 260, 20);
    {
        char ip[64] = "";
        WSADATA w;
        WSAStartup(MAKEWORD(2, 2), &w);
        SOCKET s = socket(AF_INET, SOCK_DGRAM, 0);
        sockaddr_in t{};
        t.sin_family = AF_INET;
        t.sin_port = htons(53);
        inet_pton(AF_INET, "192.168.49.1", &t.sin_addr);
        if (connect(s, (sockaddr*)&t, sizeof(t)) == 0) {
            sockaddr_in me{};
            int ml = sizeof(me);
            getsockname(s, (sockaddr*)&me, &ml);
            inet_ntop(AF_INET, &me.sin_addr, ip, sizeof(ip));
        }
        closesocket(s);
        std::wstring txt = Utf16(ip) + L":8765";
        UiText(g_app.ft_local, txt.c_str());
    }
    ty += 28;
    MakeLabel(ps, TR(L"Peer IP", L"对方IP"), 16, ty + 4, 70, 20);
    g_app.ft_pair_ip = CreateWindowW(L"EDIT", L"192.168.49.1",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL, 96, ty, 160, 24, ps,
        (HMENU)(INT_PTR)Ctl::FtPairIp, g_app.inst, nullptr);
    g_app.ft_pair_btn = CreateWindowW(L"BUTTON", TR(L"Pair", L"配对"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        270, ty - 1, 80, 26, ps, (HMENU)(INT_PTR)Ctl::FtPairBtn,
        g_app.inst, nullptr);
    MakeLabel(ps, TR(L"(both ends tap Pair)", L"（两端都要配对）"),
              360, ty + 4, 220, 20);
    ty += 32;
    // save-to row
    g_app.ft_dir_label = MakeLabel(ps, TR(L"Save to: not set", L"保存到：未设置"),
                                   16, ty, 420, 20);
    g_app.ft_dir_btn = CreateWindowW(L"BUTTON", TR(L"Folder...", L"目录…"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        450, ty - 3, 100, 26, ps, (HMENU)(INT_PTR)Ctl::FtDirBtn,
        g_app.inst, nullptr);
    ty += 32;
    g_app.ft_file = CreateWindowW(L"EDIT", L"",
        WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL, 96, ty, 330, 24, ps, nullptr,
        g_app.inst, nullptr);
    MakeLabel(ps, TR(L"File", L"文件"), 16, ty + 4, 60, 20);
    g_app.ft_browse = CreateWindowW(L"BUTTON", TR(L"Browse", L"选择文件"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        434, ty - 1, 116, 26, ps, (HMENU)(INT_PTR)Ctl::FtBrowse,
        g_app.inst, nullptr);
    ty += 34;
    g_app.ft_send = CreateWindowW(L"BUTTON",
        TR(L"Send a file to the peer", L"发送文件到对方"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        16, ty, 200, 32, ps, (HMENU)(INT_PTR)Ctl::FtSend,
        g_app.inst, nullptr);
    MarkAccent(g_app.ft_send);
    g_app.ft_recv_btn = CreateWindowW(L"BUTTON",
        TR(L"Receive from the peer", L"从对方接收文件"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        224, ty, 200, 32, ps, (HMENU)(INT_PTR)Ctl::FtRecvBtn,
        g_app.inst, nullptr);
    MarkAccent(g_app.ft_recv_btn);
    g_app.ft_cancel_btn = CreateWindowW(L"BUTTON", TR(L"Cancel", L"取消"),
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW,
        432, ty, 120, 32, ps, (HMENU)(INT_PTR)Ctl::FtCancelBtn,
        g_app.inst, nullptr);
    ty += 40;
    g_app.ft_progress = MakeLabel(ps, TR(L"Idle", L"空闲"), 16, ty, 540, 20);
    ty += 26;
    // helper card visuals
    AddCard(ps, 8, 6, kWidth - 40, y - 12);

    // ===== 加速页 (page_accel) — original layout, reparented =====
    HWND pa = g_app.page_accel;
    y = 14;
    MakeLabel(pa, TR(L"Built-in open-source Clash core - loopback micro-kernel, "
                  L"coexists with Clash Verge.",
                  L"内置开源 Clash 内核（mihomo）- 仅回环微内核，与 Clash Verge 共存。"),
           16, y, 560, 20);
    y += 26;
    g_app.sub_edit = CreateWindowW(
        L"EDIT", L"", WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL,
        16, y, 470, 26, pa, (HMENU)(INT_PTR)Ctl::SubEdit, g_app.inst, nullptr);
    MakeButton(pa, L"Paste", 494, y, 90, 26, (int)Ctl::PasteBtn);
    y += 34;
    MakeLabel(pa, L"Node", 16, y + 4, 44, 20);
    g_app.node_combo = CreateWindowW(
        L"COMBOBOX", L"", WS_VISIBLE | WS_CHILD | CBS_DROPDOWNLIST |
            CBS_OWNERDRAWFIXED | CBS_HASSTRINGS | WS_VSCROLL,
        64, y, 300, 200, pa, (HMENU)(INT_PTR)Ctl::NodeCombo, g_app.inst,
        nullptr);
    {
        // beta1.0.95: the old 60/50/60 buttons at 410/476/532 left ~6px gaps
        // next to a 340px combo — everything after "Node" looked crammed
        // (field report). Wider buttons, even spacing, aligned to the
        // 592px right edge used by the rows above/below.
        HWND b1 = MakeButton(pa, TR(L"Delay", L"延迟"), 372, y, 70, 26,
                             (int)Ctl::DelayBtn);
        HWND b2 = MakeButton(pa, TR(L"Auto", L"自动"), 448, y, 64, 26,
                             (int)Ctl::AutoBtn);
        HWND b3 = MakeButton(pa, TR(L"Refresh", L"刷新"), 518, y, 74, 26,
                             (int)Ctl::RefreshBtn);
        for (HWND b : {b1, b2, b3})
            SetWindowLongPtrW(b, GWL_STYLE,
                              WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW);
    }
    y += 34;
    MakeLabel(pa, L"Proxy URL", 16, y + 4, 70, 20);
    // beta1.0.99: empty start. The port-probe chain below discovers whatever
    // proxy is actually listening (7897/7890/7891/33210/10809/...); a stale
    // prefilled 7897 is what made every download 12029 on machines whose
    // proxy lives elsewhere. Prefill == guaranteed wrong guess.
    g_app.proxy_url = CreateWindowW(
        L"EDIT", L"", WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL,
        92, y, 492, 26, pa, (HMENU)(INT_PTR)Ctl::ProxyUrlEdit, g_app.inst, nullptr);
    y += 32;
    MakeLabel(pa, L"Proxy IP", 16, y + 4, 70, 20);
    g_app.proxy_ip = CreateWindowW(
        L"EDIT", L"127.0.0.1", WS_VISIBLE | WS_CHILD | ES_AUTOHSCROLL,
        92, y, 492, 26, pa, (HMENU)(INT_PTR)Ctl::ProxyIpEdit, g_app.inst, nullptr);
    y += 34;
    g_app.hosts_label = MakeLabel(pa, L"", 16, y + 4, 340, 20);
    {
        HWND hb = MakeButton(pa, TR(L"Repair hosts", L"修复 hosts"), 440, y,
                             144, 26, (int)Ctl::HostsFixBtn);
        SetWindowLongPtrW(hb, GWL_STYLE,
                          WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW);
    }
    y += 34;
    g_app.start_btn = MakeButton(pa, TR(L"Start Boost", L"开始加速"),
                                 16, y, 280, 32, (int)Ctl::StartBtn);
    g_app.stop_btn = MakeButton(pa, TR(L"Stop", L"停止"), 304, y, 280, 32,
                                (int)Ctl::StopBtn);
    SetWindowLongPtrW(g_app.start_btn, GWL_STYLE,
                      WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW);
    SetWindowLongPtrW(g_app.stop_btn, GWL_STYLE,
                      WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_OWNERDRAW);
    MarkAccent(g_app.start_btn);
    UiEnable(g_app.stop_btn, FALSE);
    y += 40;
    g_app.status = MakeLabel(pa, TR(L"Idle. Paste a subscription, then start.", L"空闲。粘贴订阅后开始。"),
                             16, y, 560, 20);

    ShowWindow(g_app.page_conn, SW_SHOW);
    // hosts integrity on open
    auto rep = linkhelm::InspectHosts();
    if (rep.bom_count > 0) {
        UiText(g_app.hosts_label, (L"hosts anomaly: " + std::to_wstring(rep.bom_count) +
                        L" stray characters (BOM)")
                           .c_str());
    } else {
        UiText(g_app.hosts_label, L"hosts file OK");
    }
}

void SwitchPage(int which) {  // 0=conn 1=share 2=accel
    ShowWindow(g_app.page_conn, which == 0 ? SW_SHOW : SW_HIDE);
    ShowWindow(g_app.page_share, which == 1 ? SW_SHOW : SW_HIDE);
    ShowWindow(g_app.page_accel, which == 2 ? SW_SHOW : SW_HIDE);
}

// ---- ICS share start/stop (02 panel; C++ orchestrates, WinRT tethering
// script + C++ ics module execute) ----
std::wstring RunPowerShell(const std::wstring& script, DWORD timeoutMs) {
    // beta1.0.91: inline "-Command <long script>" gets mangled by the PS
    // parser ($ vars, type literals) - the SSID prefill came back EMPTY for
    // exactly this reason (live-verified). Write the script to a temp file
    // and run it with -File: robust for any content.
    // beta1.0.95: (1) UNIQUE temp name per call — the startup SSID prefill
    // thread and an ICS start used to clobber/delete the SAME lh_run.ps1
    // under each other (real "script error" source). (2) Timeout is a
    // parameter: the hotspot start script legitimately needs ~30s
    // (StartTetheringAsync wait + adapter retry loop); the old hard 15s
    // TerminateProcess killed it mid-run — the hotspot DID turn on but the
    // GUID never printed, so the UI reported a failure. Display bug root.
    wchar_t tmp[MAX_PATH]{};
    GetTempPathW(MAX_PATH, tmp);
    std::wstring file = std::wstring(tmp) + L"lh_run_" +
        std::to_wstring(GetCurrentProcessId()) + L"_" +
        std::to_wstring(GetTickCount64()) + L".ps1";
    FILE* f = _wfopen(file.c_str(), L"wb");
    if (!f) return L"";
    fwrite("\xEF\xBB\xBF", 1, 3, f);  // UTF-8 BOM
    std::string body;
    {
        int n = WideCharToMultiByte(CP_UTF8, 0, script.c_str(),
                                    (int)script.size(), nullptr, 0,
                                    nullptr, nullptr);
        body.resize(n);
        WideCharToMultiByte(CP_UTF8, 0, script.c_str(), (int)script.size(),
                            &body[0], n, nullptr, nullptr);
    }
    fwrite(body.c_str(), 1, body.size(), f);
    fclose(f);

    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    HANDLE r = nullptr, w = nullptr;
    if (!CreatePipe(&r, &w, &sa, 0)) return L"";
    SetHandleInformation(r, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = w;
    si.hStdError = w;
    PROCESS_INFORMATION pi{};
    std::wstring cmd = L"powershell.exe -NoProfile -NonInteractive "
                       L"-ExecutionPolicy Bypass -File \"" + file + L"\"";
    std::vector<wchar_t> buf(cmd.begin(), cmd.end());
    buf.push_back(L'\0');
    if (!CreateProcessW(nullptr, buf.data(), nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(r);
        CloseHandle(w);
        return L"";
    }
    CloseHandle(w);
    std::string out;
    char rb[4096];
    DWORD n = 0;
    while (ReadFile(r, rb, sizeof(rb), &n, nullptr) && n) out.append(rb, n);
    CloseHandle(r);
    if (WaitForSingleObject(pi.hProcess, timeoutMs) == WAIT_TIMEOUT)
        TerminateProcess(pi.hProcess, 1);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    DeleteFileW(file.c_str());
    int wn = MultiByteToWideChar(CP_ACP, 0, out.c_str(), -1, nullptr, 0);
    std::wstring ws((size_t)(wn ? wn - 1 : 0), L'\0');
    if (wn) MultiByteToWideChar(CP_ACP, 0, out.c_str(), -1, &ws[0], wn);
    return ws;
}

std::string RunIcsModule(const std::wstring& args, std::wstring* error) {
    wchar_t self[MAX_PATH]{};
    GetModuleFileNameW(nullptr, self, MAX_PATH);
    std::wstring d(self);
    auto slash = d.rfind(L'\\');
    if (slash != std::wstring::npos) d = d.substr(0, slash);
    std::wstring exe = d + L"\\engine\\linkhelm_ics.exe";
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    HANDLE r = nullptr, w = nullptr;
    if (!CreatePipe(&r, &w, &sa, 0)) { if (error) *error = L"pipe"; return ""; }
    SetHandleInformation(r, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = w;
    PROCESS_INFORMATION pi{};
    std::wstring cmd = L"\"" + exe + L"\" " + args;
    if (!CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(r); CloseHandle(w);
        if (error) *error = L"C++ ICS 模块未能启动";
        return "";
    }
    CloseHandle(w);
    std::string out;
    char buf[8192];
    DWORD n = 0;
    while (ReadFile(r, buf, sizeof(buf), &n, nullptr) && n) out.append(buf, n);
    CloseHandle(r);
    if (WaitForSingleObject(pi.hProcess, 15000) == WAIT_TIMEOUT)
        TerminateProcess(pi.hProcess, 1);
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return out;
}

void WorkerIcsStart() {
    bool wifi = SendMessageW(g_app.ics_wifi, BM_GETCHECK, 0, 0) == BST_CHECKED;
    Log(wifi ? L"[ics] Wi-Fi hotspot mode: starting system hotspot..."
             : L"[ics] Ethernet mode: enumerating physical adapters...");
    std::wstring outGuid;
    std::wstring ssid, pass;

    if (wifi) {
        // beta1.0.95: EXACT beta1.0.3 START_HOTSPOT script (the version that
        // worked in the field): Find-TetheringAdapter with cloned-NIC
        // fallback (Win11 Wi-Fi 7), 192.168.137.1 identification, retry
        // loop, rollback on failure. Run via the file-based runner.
        // beta1.0.95: 45s budget — the script itself can legitimately take
        // ~30s (15s StartTetheringAsync wait + 10s adapter retry); the old
        // 15s default killed it mid-flight.
        std::wstring rep = RunPowerShell(
            L"$ErrorActionPreference='Stop';\n"
            L"function Find-TetheringAdapter {\n"
            L"  $cands = @(Get-NetAdapter -IncludeHidden | Where-Object {\n"
            L"    $_.Status -eq 'Up' -and\n"
            L"    $_.InterfaceDescription -like '*Microsoft Wi-Fi Direct Virtual Adapter*' })\n"
            L"  if ($cands.Count -gt 1) {\n"
            L"    $preferred = @($cands | Where-Object { $_.InterfaceDescription -like '*#2' })\n"
            L"    if ($preferred.Count -ge 1) { return @($preferred[0]) } }\n"
            L"  if ($cands.Count -eq 1) { return $cands }\n"
            L"  $ipEntries = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |\n"
            L"    Where-Object { $_.IPAddress -like '192.168.137.*' } |\n"
            L"    Sort-Object { $_.IPAddress -eq '192.168.137.1' } -Descending)\n"
            L"  foreach ($entry in $ipEntries) {\n"
            L"    $byIp = @(Get-NetAdapter -IncludeHidden | Where-Object {\n"
            L"      $_.Status -eq 'Up' -and $_.InterfaceIndex -eq $entry.InterfaceIndex -and\n"
            L"      $_.InterfaceDescription -notlike '*WAN Miniport*' })\n"
            L"    if ($byIp.Count -eq 1) { return $byIp } }\n"
            L"  return @() }\n"
            L"$started=$false; $manager=$null; $profile=$null\n"
            L"try {\n"
            L"  $profile=[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()\n"
            L"  if (-not $profile) { Write-Output 'ERR Windows has no internet connection profile. Connect first, then retry.'; exit }\n"
            L"  $manager=[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)\n"
            L"  if (-not $manager) { Write-Output 'ERR Windows Mobile Hotspot unavailable. Use Ethernet.'; exit }\n"
            L"  if ($manager.TetheringOperationalState.ToString() -ne 'On') {\n"
            L"    $manager.StartTetheringAsync() | Out-Null; $started=$true\n"
            L"    for ($i=0; $i -lt 30 -and $manager.TetheringOperationalState.ToString() -ne 'On'; $i++) { Start-Sleep -Milliseconds 500 } }\n"
            L"  if ($manager.TetheringOperationalState.ToString() -ne 'On') { Write-Output 'ERR hotspot did not start'; exit }\n"
            L"  $adapters=@()\n"
            L"  for ($i=0; $i -lt 5; $i++) {\n"
            L"    $adapters = @(Find-TetheringAdapter)\n"
            L"    if ($adapters.Count -eq 1) { break }; Start-Sleep -Seconds 2 }\n"
            L"  if ($adapters.Count -ne 1) { Write-Output 'ERR could not identify hotspot adapter (cloned NIC). Sharing skipped; system NAT carries it.'; exit }\n"
            L"  $config = $manager.GetCurrentAccessPointConfiguration()\n"
            L"  Write-Output ('GUID:'+$adapters[0].InterfaceGuid.ToString())\n"
            L"  Write-Output ('SSID:'+[string]$config.Ssid)\n"
        L"  Write-Output ('PASS:'+[string]$config.Passphrase)\n"
        L"} catch {\n"
        L"  if ($started -and $manager) { try { $manager.StopTetheringAsync() | Out-Null } catch {} }\n"
        L"  Write-Output ('ERR '+$_.Exception.Message) }\n",
            45000);
        size_t pe = rep.find(L"ERR");
        if (pe != std::wstring::npos) {
            // beta1.0.95 DISPLAY FIX: the script may have TIMED OUT after the
            // hotspot actually turned on (the old 15s cap) or skipped sharing
            // on a cloned NIC — do NOT trust the error text for the UI state.
            // Ask the system for the REAL tethering state and reflect THAT.
            std::wstring probe = RunPowerShell(
                L"$p=[Windows.Networking.Connectivity.NetworkInformation,"
                L"Windows.Networking.Connectivity,ContentType=WindowsRuntime]"
                L"::GetInternetConnectionProfile();\n"
                L"if($p){$m=[Windows.Networking.NetworkOperators."
                L"NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,"
                L"ContentType=WindowsRuntime]::CreateFromConnectionProfile($p);\n"
                L"Write-Output ('STATE:'+$m.TetheringOperationalState.ToString())}\n");
            bool hotspotOn = probe.find(L"STATE:On") != std::wstring::npos;
            if (hotspotOn) {
                // beta1.0.95: the hotspot IS on — "ERR ..." in the log next to
                // a green "sharing on" looked like a failure (field report).
                // Downgrade to an informational line; the raw error text stays
                // out of the log on this path.
                Log(L"[ics] hotspot ON — sharing layer skipped (cloned NIC), "
                    L"system NAT carries it");
                UiText(g_app.ics_info, TR(L"Sharing on (system NAT, cloned NIC)",
                       L"共享已开启（系统NAT，克隆网卡）"));
                UiEnable(g_app.ics_start, FALSE);
                UiEnable(g_app.ics_stop, TRUE);
            } else {
                Log(L"[ics] " + rep.substr(pe, 220));
                UiText(g_app.ics_info, TR(L"Hotspot start failed — see log",
                       L"热点启动失败——详见日志"));
            }
            g_app.ics_busy.store(false); return;
        }
        size_t p1 = rep.find(L"GUID:");
        if (p1 != std::wstring::npos) {
            size_t e = rep.find(L'\n', p1);
            outGuid = rep.substr(p1 + 5, e == std::wstring::npos
                                            ? std::wstring::npos : e - p1 - 5);
            while (!outGuid.empty() && (outGuid.back() == L'\r' ||
                                        outGuid.back() == L' '))
                outGuid.pop_back();
        }
        size_t p2 = rep.find(L"SSID:");
        if (p2 != std::wstring::npos) {
            size_t e = rep.find(L'\n', p2);
            ssid = rep.substr(p2 + 5, e == std::wstring::npos
                                          ? std::wstring::npos : e - p2 - 5);
            while (!ssid.empty() && (ssid.back() == L'\r' || ssid.back() == L' '))
                ssid.pop_back();
        }
        size_t p3 = rep.find(L"PASS:");
        if (p3 != std::wstring::npos) {
            size_t e = rep.find(L'\n', p3);
            pass = rep.substr(p3 + 5, e == std::wstring::npos
                                           ? std::wstring::npos : e - p3 - 5);
            while (!pass.empty() && (pass.back() == L'\r' || pass.back() == L' '))
                pass.pop_back();
        }
        if (outGuid.empty()) {
            Log(L"[ics] hotspot start failed: " + rep.substr(0, 200));
            g_app.ics_busy.store(false); return;
        }
        Log(L"[ics] hotspot started SSID=" + ssid);
        if (g_app.ics_ssid) UiText(g_app.ics_ssid, ssid.c_str());
        if (g_app.ics_pass) UiText(g_app.ics_pass, pass.c_str());
    } else {
        int sel = (int)SendMessageW(g_app.ics_eth_combo, CB_GETCURSEL, 0, 0);
        if (sel < 0) {
            Log(L"[ics] select an Ethernet adapter first");
            UiText(g_app.ics_info, TR(L"Pick an adapter in the list first",
                   L"请先在下拉列表选择适配器"));
            g_app.ics_busy.store(false); return;
        }
        wchar_t guid[128]{};
        SendMessageW(g_app.ics_eth_combo, CB_GETLBTEXT, sel, (LPARAM)guid);
        std::wstring g(guid);
        auto sp = g.find(L'|');
        if (sp != std::wstring::npos) outGuid = g.substr(0, sp);
        else outGuid = g;
    }
    // normalize: upper-case, strip braces/spaces
    for (auto& c : outGuid) c = towupper(c);
    while (!outGuid.empty() && outGuid.front() == L'{') outGuid.erase(0, 1);
    while (!outGuid.empty() && (outGuid.front() == L' ')) outGuid.erase(0, 1);
    auto cbr = outGuid.find(L'}');
    if (cbr != std::wstring::npos) outGuid.erase(cbr, 1);

    std::string out8 = RunIcsModule(L"ENABLE " + outGuid, nullptr);
    std::wstring out = Utf16(out8);
    if (out8.find("\"error\"") != std::string::npos) {
        Log(L"[ics] sharing enable failed: " + out.substr(0, 300));
        // beta1.0.95: the hotspot itself is on (script got this far) — probe
        // and reflect reality instead of leaving a stale "off" display.
        std::wstring probe2 = RunPowerShell(
            L"$p=[Windows.Networking.Connectivity.NetworkInformation,"
            L"Windows.Networking.Connectivity,ContentType=WindowsRuntime]"
            L"::GetInternetConnectionProfile();\n"
            L"if($p){$m=[Windows.Networking.NetworkOperators."
            L"NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,"
            L"ContentType=WindowsRuntime]::CreateFromConnectionProfile($p);\n"
            L"Write-Output ('STATE:'+$m.TetheringOperationalState.ToString())}\n");
        if (probe2.find(L"STATE:On") != std::wstring::npos) {
            UiText(g_app.ics_info, TR(L"Sharing on (system NAT, cloned NIC)",
                   L"共享已开启（系统NAT，克隆网卡）"));
            UiEnable(g_app.ics_start, FALSE);
            UiEnable(g_app.ics_stop, TRUE);
        }
        g_app.ics_busy.store(false); return;
    }
    bool skipped = out8.find("ics_skipped") != std::string::npos;
    g_app.ics_state = L"{\"owned\":true,\"source_guid\":\"\",\"output_guid\":\"" +
                      outGuid + L"\",\"previous\":[]}";
    if (skipped) {
        Log(L"[ics] adapter not in sharing list (cloned NIC); system NAT carries it");
        UiText(g_app.ics_info, (std::wstring(TR(L"Sharing on (system NAT) ",
                                        L"共享已开启（系统 NAT）")) + ssid)
                           .c_str());
    } else {
        Log(L"[ics] sharing enabled (LinkHelm-IwIP -> output adapter)");
        UiText(g_app.ics_info, (std::wstring(TR(L"Sharing on ", L"共享已开启 ")) +
                        (ssid.empty() ? TR(L"Ethernet", L"以太网") : ssid))
                           .c_str());
    }
    UiEnable(g_app.ics_start, FALSE);
    UiEnable(g_app.ics_stop, TRUE);
    g_app.ics_busy.store(false);
}

void WorkerIcsStop() {
    if (!g_app.ics_state.empty()) {
        RunIcsModule(L"DISABLE " + g_app.ics_state, nullptr);
        g_app.ics_state.clear();
    }
    RunPowerShell(
        L"$p=[Windows.Networking.Connectivity.NetworkInformation,"
        L"Windows.Networking.Connectivity,ContentType=WindowsRuntime]"
        L"::GetInternetConnectionProfile();"
        L"if($p){$m=[Windows.Networking.NetworkOperators."
        L"NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,"
        L"ContentType=WindowsRuntime]::CreateFromConnectionProfile($p);"
        L"if($m.TetheringOperationalState.ToString() -eq 'On')"
        L"{$m.StopTetheringAsync()|Out-Null}}",
        45000);
    // beta1.0.95: StopTetheringAsync is async — give it a moment, then
    // verify; only show "off" when the system REALLY turned it off.
    {
        std::wstring probe3 = RunPowerShell(
            L"Start-Sleep -Seconds 2;"
            L"$p=[Windows.Networking.Connectivity.NetworkInformation,"
            L"Windows.Networking.Connectivity,ContentType=WindowsRuntime]"
            L"::GetInternetConnectionProfile();"
            L"if($p){$m=[Windows.Networking.NetworkOperators."
            L"NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,"
            L"ContentType=WindowsRuntime]::CreateFromConnectionProfile($p);"
            L"Write-Output ('STATE:'+$m.TetheringOperationalState.ToString())}",
            20000);
        if (probe3.find(L"STATE:On") != std::wstring::npos) {
            Log(L"[ics] stop: hotspot still on (async) — will settle shortly");
            UiText(g_app.ics_info, TR(L"Stopping… (tap Stop again if it stays on)",
                   L"停止中…（若仍开启请再点一次）"));
            g_app.ics_busy.store(false); return;
        }
    }
    Log(L"[ics] sharing stopped and restored");
    UiText(g_app.ics_info, TR(L"Sharing off", L"未开启共享"));
    if (g_app.ics_ssid) UiText(g_app.ics_ssid, L"");
    if (g_app.ics_pass) UiText(g_app.ics_pass, L"");
    UiEnable(g_app.ics_start, TRUE);
    UiEnable(g_app.ics_stop, FALSE);
    g_app.ics_busy.store(false);
}

void RefreshEthList() {
    SendMessageW(g_app.ics_eth_combo, CB_RESETCONTENT, 0, 0);
    // beta1.0.95: also default-select the first entry so Start Sharing
    // works immediately after choosing Ethernet.

    std::string out = RunIcsModule(L"LIST_ETHERNET", nullptr);
    // parse [{"guid":"..","name":".."}] minimal
    size_t pos = 0;
    while ((pos = out.find("\"guid\":\"", pos)) != std::string::npos) {
        size_t s = pos + 8;
        size_t e = out.find('"', s);
        std::string guid = out.substr(s, e - s);
        size_t np = out.find("\"name\":\"", e);
        std::string name;
        if (np != std::string::npos) {
            size_t ns = np + 8;
            size_t ne = out.find('"', ns);
            name = out.substr(ns, ne - ns);
        }
        std::wstring line = Utf16(guid) + L" | " + Utf16(name);
        SendMessageW(g_app.ics_eth_combo, CB_ADDSTRING, 0, (LPARAM)line.c_str());
        pos = e;
    }
    if (SendMessageW(g_app.ics_eth_combo, CB_GETCOUNT, 0, 0) > 0)
        SendMessageW(g_app.ics_eth_combo, CB_SETCURSEL, 0, 0);
}

// ---- connect/disconnect workers (pure C++ tunnel manager) ----
void WorkerConnect() {
    linkhelm::TunnelConfig cfg;
    cfg.phone_ip = GetText(g_app.phone_ip);
    cfg.phone_port = _wtoi(GetText(g_app.phone_port).c_str());
    cfg.socks_user = GetText(g_app.phone_user);
    cfg.socks_pass = GetText(g_app.phone_pass);
    if (cfg.phone_ip.empty() || cfg.phone_port <= 0) {
        Log(L"Enter the phone IP and port");
        return;
    }
    g_app.tun = std::make_unique<linkhelm::TunnelManager>(
        [](const std::wstring& m) { Log(m); },
        [](const std::wstring& m) {
            if (g_app.conn_status) UiText(g_app.conn_status, m.c_str());
        });
    std::wstring err;
    bool ok = g_app.tun->Start(cfg, &err);
    if (ok) {
        Log(L"[tun] connected (engine kernel-managed in C++ Job Object)");
        UiEnable(g_app.conn_btn, FALSE);
        UiEnable(g_app.disc_btn, TRUE);
    } else {
        Log(L"[tun] connect failed: " + err);
        g_app.tun.reset();
    }
}

void WorkerDisconnect() {
    if (g_app.tun) {
        g_app.tun->Stop();
        g_app.tun.reset();
        Log(L"[tun] disconnected, routes cleaned");
    }
    UiEnable(g_app.conn_btn, TRUE);
    UiEnable(g_app.disc_btn, FALSE);
}

// ---- file transfer worker ----
void WorkerSendFile() {
    // (busy flag is pre-set by the button handler — double-click guard)
    std::wstring target = GetText(g_app.ft_pair_ip);
    std::wstring file = GetText(g_app.ft_file);
    if (target.empty() || file.empty()) {
        Log(L"[ft] enter the target IP and pick a file");
        g_app.ft_busy.store(false);
        return;
    }
    // beta1.0.87 (restored beta1.0.95 — do NOT remove): transfers ride the
    // DIRECT hotspot link, never the TUN. Add a host route to the peer via
    // the current default gateway; harmless if it already exists.
    {
        std::string rout;
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE rp = nullptr, wp2 = nullptr;
        if (CreatePipe(&rp, &wp2, &sa, 0)) {
            SetHandleInformation(rp, HANDLE_FLAG_INHERIT, 0);
            STARTUPINFOW si{};
            si.cb = sizeof(si);
            si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
            si.wShowWindow = SW_HIDE;
            si.hStdOutput = wp2;
            si.hStdError = wp2;
            PROCESS_INFORMATION pi{};
            std::wstring cmdr = L"route print -4";
            if (CreateProcessW(nullptr, &cmdr[0], nullptr, nullptr, TRUE,
                               CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
                CloseHandle(wp2);
                char b2[4096];
                DWORD n2 = 0;
                while (ReadFile(rp, b2, sizeof(b2), &n2, nullptr) && n2)
                    rout.append(b2, n2);
                CloseHandle(rp);
                WaitForSingleObject(pi.hProcess, 5000);
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                // beta1.0.88: spacing-independent scan — line STARTS with
                // "0.0.0.0" and 2nd token is "0.0.0.0"; 3rd token = gateway.
                size_t ls = 0;
                std::string gw;
                while (ls < rout.size() && gw.empty()) {
                    size_t le = rout.find('\n', ls);
                    if (le == std::string::npos) le = rout.size();
                    size_t t = rout.find_first_not_of(" \t", ls);
                    if (t != std::string::npos && t < le &&
                        rout.compare(t, 7, "0.0.0.0") == 0 &&
                        rout.compare(t + 7, 1, " ") == 0) {
                        int tok = 0;
                        size_t q = t;
                        while (tok <= 2 && q < le) {
                            while (q < le && rout[q] == ' ') ++q;
                            size_t st = q;
                            while (q < le && rout[q] != ' ') ++q;
                            if (tok == 1 &&
                                rout.compare(st, 7, "0.0.0.0") != 0) break;
                            if (tok == 2) gw = rout.substr(st, q - st);
                            ++tok;
                        }
                    }
                    ls = le + 1;
                }
                bool ip = !gw.empty() && gw.find('.') != std::string::npos &&
                          gw.find_first_not_of("0123456789.") ==
                              std::string::npos;
                if (ip) {
                    std::wstring add = L"route add " + target +
                                       L" mask 255.255.255.255 " +
                                       Utf16(gw) + L" metric 1";
                    STARTUPINFOW s2{};
                    s2.cb = sizeof(s2);
                    s2.dwFlags = STARTF_USESHOWWINDOW;
                    s2.wShowWindow = SW_HIDE;
                    PROCESS_INFORMATION p2{};
                    CreateProcessW(nullptr, &add[0], nullptr, nullptr,
                                   FALSE, CREATE_NO_WINDOW, nullptr,
                                   nullptr, &s2, &p2);
                    if (p2.hProcess) {
                        WaitForSingleObject(p2.hProcess, 5000);
                        CloseHandle(p2.hThread);
                        CloseHandle(p2.hProcess);
                    }
                    Log(L"[ft] peer pinned to the direct link via " +
                        Utf16(gw));
                } else {
                    Log(L"[ft] default gateway not found; sending as-is");
                }
            } else {
                CloseHandle(rp);
                CloseHandle(wp2);
            }
        }
    }
    linkhelm::FileTransfer ft([](const std::wstring& m) { Log(L"[ft] " + m); });
    auto progress = [](unsigned long long sent, unsigned long long total) {
        static unsigned long long lastPost = 0;
        if (sent - lastPost >= 1024 * 1024 || sent == total) {
            lastPost = sent;
            wchar_t buf[128];
            swprintf(buf, 127, L"Progress: %.1f / %.1f MB",
                     sent / 1048576.0, total / 1048576.0);
            if (g_app.ft_progress) UiText(g_app.ft_progress, buf);
        }
    };
    std::string ip8 = Utf8(target);
    std::wstring uw = GetText(g_app.phone_user);
    std::wstring pw = GetText(g_app.phone_pass);
    if (uw.empty()) uw = L"linkhelm01";
    if (pw.empty()) pw = L"k7h2m9x4qa";
    bool ok = ft.SendFile(ip8, 8765, Utf8(uw), Utf8(pw), file, progress);
    if (g_app.ft_progress)
        UiText(g_app.ft_progress, ok ? L"发送完成 ✓" : L"发送失败");
    g_app.ft_busy.store(false);   // re-arm the button
}

// settings window: dark bg, rounded cards, owner-draw children (radios
// write lang.txt; checkbox is informational; 9 = close)
LRESULT CALLBACK SettingsProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
        case WM_CTLCOLORBTN:
        case WM_CTLCOLORSTATIC: {
            HDC dc = (HDC)w;
            SetTextColor(dc, kText);
            SetBkColor(dc, kBg);
            return (LRESULT)g_brBg;
        }
        case WM_APP + 30: {
            // init toggle states (registry -> autostart; autoconnect ON)
            struct ReflState { HWND autoChk; HWND reconChk; };
            ReflState* rs = (ReflState*)l;
            bool autoOn = false;
            HKEY k;
            if (RegOpenKeyExW(HKEY_CURRENT_USER,
                              L"Software\\Microsoft\\Windows"
                              L"\\CurrentVersion\\Run", 0,
                              KEY_READ, &k) == ERROR_SUCCESS) {
                if (RegQueryValueExW(k, L"LinkHelm", nullptr, nullptr,
                                     nullptr, nullptr) == ERROR_SUCCESS)
                    autoOn = true;
                RegCloseKey(k);
            }
            SetPropW(rs->autoChk, L"lhOn", (HANDLE)(autoOn ? 1 : 0));
            SetPropW(rs->reconChk, L"lhOn", (HANDLE)1);
            InvalidateRect(rs->autoChk, nullptr, TRUE);
            InvalidateRect(rs->reconChk, nullptr, TRUE);
            return 0;
        }
        case WM_DRAWITEM: {
            DRAWITEMSTRUCT* d = (DRAWITEMSTRUCT*)l;
            if (d && d->CtlType == ODT_STATIC) {
                HANDLE st = GetPropW(d->hwndItem, L"lhOn");
                if (st != nullptr || d->itemID == 0) {
                    // toggle row: text + CTk switch, NO frame
                    bool on = st == (HANDLE)1;
                    RECT r = d->rcItem;
                    SetBkMode(d->hDC, TRANSPARENT);
                    SetTextColor(d->hDC, kText);
                    RECT tr2 = r;
                    tr2.right -= 60;
                    DrawTextW(d->hDC, L"", -1, &tr2, DT_LEFT | DT_VCENTER);
                    // window text for the label
                    wchar_t lab[96];
                    GetWindowTextW(d->hwndItem, lab, 96);
                    DrawTextW(d->hDC, lab, -1, &tr2,
                              DT_LEFT | DT_VCENTER | DT_SINGLELINE |
                                  DT_END_ELLIPSIS);
                    int cy = (r.top + r.bottom) / 2;
                    RECT trk{r.right - 52, cy - 10, r.right - 4, cy + 10};
                    HBRUSH trkBr = CreateSolidBrush(
                        on ? kAccent : RGB(70, 70, 78));
                    HRGN pill = CreateRoundRectRgn(
                        trk.left, trk.top, trk.right + 1, trk.bottom + 1,
                        20, 20);
                    FillRgn(d->hDC, pill, trkBr);
                    DeleteObject(trkBr);
                    DeleteObject(pill);
                    int kd = 16;
                    int kx = on ? trk.right - kd - 2 : trk.left + 2;
                    HBRUSH kb = CreateSolidBrush(RGB(255, 255, 255));
                    HGDIOBJ ok2 = SelectObject(d->hDC, kb);
                    HGDIOBJ op2 = SelectObject(d->hDC,
                                               GetStockObject(NULL_PEN));
                    Ellipse(d->hDC, kx, cy - kd / 2, kx + kd, cy + kd / 2);
                    SelectObject(d->hDC, op2);
                    SelectObject(d->hDC, ok2);
                    DeleteObject(kb);
                    return TRUE;
                }
                // card background (no border stroke - user wants no frames)
                HRGN rg = CreateRoundRectRgn(d->rcItem.left, d->rcItem.top,
                                             d->rcItem.right + 1,
                                             d->rcItem.bottom + 1, 10, 10);
                HBRUSH br = CreateSolidBrush(kCard);
                FillRgn(d->hDC, rg, br);
                DeleteObject(br);
                DeleteObject(rg);
                return TRUE;
            }
            if (DrawDarkCtl(d)) return TRUE;
            break;
        }
        case WM_ERASEBKGND: {
            RECT rc;
            GetClientRect(h, &rc);
            FillRect((HDC)w, &rc, g_brBg);
            return TRUE;
        }
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(h, &ps);
            // beta1.0.91: flat settings page — only the base background,
            // no card boxes/borders (user: keep just the base color)
            EndPaint(h, &ps);
            return 0;
        }
        case WM_COMMAND: {
            // beta1.0.95 CRITICAL: BN_CLICKED has HIWORD==0 too, and lParam
            // is non-null for BOTH — buttons were being eaten by the toggle
            // branch (Close/radios dead for 5 builds). Distinguish by class:
            // toggle rows are STATICs, real buttons are BUTTONs.
            HWND srcH = (HWND)l;
            wchar_t srcCls[16]{};
            if (srcH) GetClassNameW(srcH, srcCls, 15);
            bool isStaticToggle = srcH && wcscmp(srcCls, L"Static") == 0;
            if (isStaticToggle) {
                int id = LOWORD(w);
                HWND th = srcH;
                HANDLE cur = GetPropW(th, L"lhOn");
                bool on = cur != nullptr && cur == (HANDLE)1;
                SetPropW(th, L"lhOn", (HANDLE)(on ? 0 : 1));
                InvalidateRect(th, nullptr, TRUE);
                if (id == (int)Ctl::TglAutostart) {
                    // WinAPI direct (no reg.exe dependency/blacklist issues)
                    bool nowOn = !on;
                    HKEY rk;
                    if (nowOn) {
                        wchar_t selfA[MAX_PATH]{};
                        GetModuleFileNameW(nullptr, selfA, MAX_PATH);
                        if (RegCreateKeyExW(HKEY_CURRENT_USER,
                                            L"Software\\Microsoft\\Windows"
                                            L"\\CurrentVersion\\Run", 0,
                                            nullptr, 0, KEY_SET_VALUE, nullptr,
                                            &rk, nullptr) == ERROR_SUCCESS) {
                            RegSetValueExW(rk, L"LinkHelm", 0, REG_SZ,
                                           (const BYTE*)selfA,
                                           (DWORD)((wcslen(selfA) + 1) *
                                                   sizeof(wchar_t)));
                            RegCloseKey(rk);
                        }
                    } else {
                        if (RegOpenKeyExW(HKEY_CURRENT_USER,
                                          L"Software\\Microsoft\\Windows"
                                          L"\\CurrentVersion\\Run", 0,
                                          KEY_SET_VALUE, &rk) ==
                            ERROR_SUCCESS) {
                            RegDeleteValueW(rk, L"LinkHelm");
                            RegCloseKey(rk);
                        }
                    }
                } else if (id == (int)Ctl::TglAutoconnect) {
                    bool nowOn = !on;
                    wchar_t selfC[MAX_PATH]{};
                    GetModuleFileNameW(nullptr, selfC, MAX_PATH);
                    std::wstring p(selfC);
                    auto sp = p.rfind(L'\\');
                    if (sp != std::wstring::npos) p = p.substr(0, sp);
                    p += L"\\autoconnect.txt";
                    if (nowOn) {
                        FILE* fc = _wfopen(p.c_str(), L"w");
                        if (fc) { fputws(L"on", fc); fclose(fc); }
                    } else {
                        DeleteFileW(p.c_str());
                    }
                }
                return 0;
            }
            if (HIWORD(w) == BN_CLICKED) {
                int id = LOWORD(w);
                if (id == 20 || id == 21) {
                    wchar_t self[MAX_PATH]{};
                    GetModuleFileNameW(nullptr, self, MAX_PATH);
                    std::wstring p(self);
                    auto sp = p.rfind(L'\\');
                    if (sp != std::wstring::npos) p = p.substr(0, sp);
                    p += L"\\lang.txt";
                    FILE* f = _wfopen(p.c_str(), L"w");
                    if (f) {
                        fputws(id == 21 ? L"zh" : L"en", f);
                        fclose(f);
                    }
                    // beta1.0.88: language switch restarts the app so every
                    // surface (window + tray) flips immediately.
                    // beta1.0.95: DELAYED relaunch — the direct restart made
                    // the new (Chinese) window appear while the old (English)
                    // one was still on screen fading out. The wrapper waits
                    // 1s first: the old window fully exits, THEN the new one
                    // comes up (user-requested pacing, not instantaneous).
                    MessageBoxW(h,
                        g_lang_zh ? L"语言已保存，程序将重启生效。"
                                  : L"Language saved. The app will restart.",
                        L"LinkHelm", MB_OK | MB_ICONINFORMATION);
                    wchar_t selfL[MAX_PATH]{};
                    GetModuleFileNameW(nullptr, selfL, MAX_PATH);
                    std::wstring relaunch = L"cmd.exe /c timeout /t 1 /nobreak >nul & \"";
                    relaunch += selfL;
                    relaunch += L"\"";
                    STARTUPINFOW siL{};
                    siL.cb = sizeof(siL);
                    PROCESS_INFORMATION piL{};
                    CreateProcessW(nullptr, &relaunch[0], nullptr, nullptr,
                                   FALSE, CREATE_NO_WINDOW, nullptr, nullptr,
                                   &siL, &piL);
                    if (piL.hProcess) {
                        CloseHandle(piL.hThread);
                        CloseHandle(piL.hProcess);
                    }
                    PostQuitMessage(0);
                    return 0;
                }
                if (id == 22) {
                    // live toggle: create/remove the logon task now
                    bool on = SendMessageW((HWND)l, BM_GETCHECK, 0, 0) ==
                              BST_CHECKED;
                    wchar_t self[MAX_PATH]{};
                    GetModuleFileNameW(nullptr, self, MAX_PATH);
                    if (on) {
                        std::wstring a = L"schtasks /Create /TN LinkHelmLogon "
                                         L"/TR \"" + std::wstring(self) +
                                         L"\" /SC ONLOGON /F";
                        // fall back to RunOnce (no admin needed)
                        std::wstring reg = L"reg add HKCU\Software\Microsoft"
                                           L"\Windows\CurrentVersion\Run "
                                           L"/v LinkHelm /t REG_SZ /d \"" +
                                           std::wstring(self) + L"\" /f";
                        RunPowerShell(reg);
                    } else {
                        RunPowerShell(L"reg delete HKCU\Software\Microsoft"
                                      L"\Windows\CurrentVersion\Run "
                                      L"/v LinkHelm /f");
                    }
                    return 0;
                }
                if (id == 23) {
                    bool on = SendMessageW((HWND)l, BM_GETCHECK, 0, 0) ==
                              BST_CHECKED;
                    wchar_t self2[MAX_PATH]{};
                    GetModuleFileNameW(nullptr, self2, MAX_PATH);
                    std::wstring p2(self2);
                    auto sp2 = p2.rfind(L'\\');
                    if (sp2 != std::wstring::npos) p2 = p2.substr(0, sp2);
                    p2 += L"\\autoconnect.txt";
                    if (on) {
                        FILE* f2 = _wfopen(p2.c_str(), L"w");
                        if (f2) { fputws(L"on", f2); fclose(f2); }
                    } else {
                        DeleteFileW(p2.c_str());
                    }
                    return 0;
                }
                if (id == 9) { DestroyWindow(h); return 0; }
            }
            break;
        }
        case WM_CLOSE:
            DestroyWindow(h);
            return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    // Tray icon messages arrive here: forward them, everything else is ours.
    if (g_app.tray && g_app.tray->HandleIconMessage(msg, wp, lp)) return 0;
    switch (msg) {
        case WM_CREATE:
            BuildUi(hwnd);
            return 0;
        case WM_COMMAND:
            switch (LOWORD(wp)) {
                case (int)Ctl::TabConn:
                case (int)Ctl::TabShare:
                case (int)Ctl::TabAccel:
                    if (HIWORD(wp) == BN_CLICKED) {
                        SwitchPage(LOWORD(wp) == (int)Ctl::TabConn ? 0
                                  : LOWORD(wp) == (int)Ctl::TabShare ? 1 : 2);
                    }
                    return 0;
                case (int)Ctl::ConnBtn:
                    UiText(g_app.conn_status, TR(L"Status: connecting…", L"状态：连接中…"));
                    std::thread(WorkerConnect).detach();
                    return 0;
                case (int)Ctl::DiscBtn:
                    // beta1.0.95: Stop() joins the monitor + runs netsh/route
                    // cleanup (10-20s). On the UI thread that froze the whole
                    // window ("无响应"). Background it; buttons flip instantly.
                    UiEnable(g_app.disc_btn, FALSE);
                    UiText(g_app.conn_status, TR(L"Status: disconnecting…", L"状态：断开中…"));
                    std::thread([] {
                        WorkerDisconnect();
                        UiEnable(g_app.conn_btn, TRUE);
                        UiText(g_app.conn_status, TR(L"Status: not connected", L"状态：未连接"));
                    }).detach();
                    return 0;
                case (int)Ctl::FtBrowse: {
                    OPENFILENAMEW ofn{};
                    wchar_t file[MAX_PATH]{};
                    ofn.lStructSize = sizeof(ofn);
                    ofn.hwndOwner = hwnd;
                    ofn.lpstrFile = file;
                    ofn.nMaxFile = MAX_PATH;
                    ofn.lpstrTitle = L"选择要发送的文件";
                    ofn.Flags = OFN_FILEMUSTEXIST | OFN_HIDEREADONLY;
                    if (GetOpenFileNameW(&ofn)) {
                        UiText(g_app.ft_file, file);
                        UiEnable(g_app.ft_send, TRUE);
                        UiText(g_app.ft_progress, TR(L"Ready to send", L"已选文件，可发送"));
                    }
                    return 0;
                }
                case (int)Ctl::FtSend:
                    // beta1.0.95: double-click guard — a second worker while
                    // the first runs raced the route/netsh steps (crashes).
                    if (g_app.ft_busy.exchange(true)) return 0;
                    UiText(g_app.ft_progress, L"发送中…");
                    std::thread([]{ WorkerSendFile(); }).detach();
                    return 0;
                case (int)Ctl::IcsStartBtn:
                    if (g_app.ics_busy.exchange(true)) return 0;
                    std::thread(WorkerIcsStart).detach();
                    return 0;
                case (int)Ctl::IcsStopBtn:
                    if (g_app.ics_busy.exchange(true)) return 0;
                    std::thread(WorkerIcsStop).detach();
                    return 0;
                case (int)Ctl::IcsModeEth:
                    if (HIWORD(wp) == BN_CLICKED) {
                        RefreshEthList();
                        UiText(g_app.ics_info, TR(L"Ethernet mode — pick an adapter below",
                               L"以太网模式——请在下方选择网卡"));
                    }
                    return 0;
                case (int)Ctl::IcsModeWifi:
                    if (HIWORD(wp) == BN_CLICKED) {
                        UiText(g_app.ics_info, TR(L"Wi-Fi hotspot mode", L"Wi-Fi 热点模式"));
                    }
                    return 0;
                case (int)Ctl::PassEye: {
                    // toggle password visibility
                    LONG st = GetWindowLongW(g_app.phone_pass, GWL_STYLE);
                    bool hidden = (st & ES_PASSWORD) != 0;
                    SendMessageW(g_app.phone_pass, EM_SETPASSWORDCHAR,
                                 hidden ? 0 : (WPARAM)L'\u25CF', 0);
                    SetWindowLongW(g_app.phone_pass, GWL_STYLE,
                                   hidden ? (st & ~ES_PASSWORD)
                                          : (st | ES_PASSWORD));
                    InvalidateRect(g_app.phone_pass, nullptr, TRUE);
                    InvalidateRect(g_app.pass_eye, nullptr, TRUE);
                    return 0;
                }
                case (int)Ctl::UdpBtn: {
                    std::thread([] {
                        Log(L"[udp] sending DNS probe via tunnel to 223.5.5.5:53...");
                        // minimal UDP DNS A query for example.com
                        WSADATA w;
                        WSAStartup(MAKEWORD(2, 2), &w);
                        SOCKET s = socket(AF_INET, SOCK_DGRAM, 0);
                        sockaddr_in a{};
                        a.sin_family = AF_INET;
                        a.sin_port = htons(53);
                        inet_pton(AF_INET, "223.5.5.5", &a.sin_addr);
                        // beta1.0.86: bind to the TUN only if it exists;
                        // a failed bind must NOT kill the probe (it used to
                        // make the socket unusable -> false "no reply").
                        sockaddr_in src{};
                        src.sin_family = AF_INET;
                        src.sin_port = 0;
                        inet_pton(AF_INET, "10.0.0.2", &src.sin_addr);
                        if (bind(s, (sockaddr*)&src, sizeof(src)) != 0) {
                            // tunnel down: rebind to any interface
                            closesocket(s);
                            s = socket(AF_INET, SOCK_DGRAM, 0);
                            u_long nb2 = 1;
                            ioctlsocket(s, FIONBIO, &nb2);
                        }
                        unsigned char q[] = {0x12,0x34,1,0,0,1,0,0,0,0,0,0,
                                             7,'e','x','a','m','p','l','e',3,'c','o','m',0,0,1,0,1};
                        sendto(s, (const char*)q, sizeof(q), 0, (sockaddr*)&a, sizeof(a));
                        char buf[512];
                        fd_set r;
                        FD_ZERO(&r);
                        FD_SET(s, &r);
                        timeval tv{4, 0};
                        int n = -1;
                        if (select(0, &r, nullptr, nullptr, &tv) > 0)
                            n = recvfrom(s, buf, sizeof(buf), 0, nullptr, nullptr);
                        closesocket(s);
                        Log(n > 0 ? L"[udp] UDP data plane OK (DNS reply received)"
                                  : L"[udp] no reply (tunnel down or UDP blocked)");
                    }).detach();
                    return 0;
                }
                case (int)Ctl::IcsBtn: {
                    if (HIWORD(wp) == BN_CLICKED) {
                        bool on = SendMessageW(g_app.ics_btn, BM_GETCHECK, 0, 0) ==
                                  BST_CHECKED;
                        if (on) {
                            Log(L"[ics] enabling sharing via the C++ ICS module...");
                            std::thread([] {
                                // pick the first connected physical ethernet
                                // (the C++ module enumerates exactly those)
                                wchar_t self[MAX_PATH]{};
                                GetModuleFileNameW(nullptr, self, MAX_PATH);
                                std::wstring d(self);
                                auto slash = d.rfind(L'\\');
                                if (slash != std::wstring::npos)
                                    d = d.substr(0, slash);
                                std::wstring ics = d + L"\\engine\\linkhelm_ics.exe";
                                STARTUPINFOW si{}; si.cb = sizeof(si);
                                si.dwFlags = STARTF_USESHOWWINDOW;
                                si.wShowWindow = SW_HIDE;
                                PROCESS_INFORMATION pi{};
                                std::wstring cmd = L"\"" + ics +
                                    L"\" ENABLE list-first-ethernet";
                                if (CreateProcessW(nullptr, &cmd[0], nullptr,
                                                   nullptr, FALSE,
                                                   CREATE_NO_WINDOW, nullptr,
                                                   nullptr, &si, &pi)) {
                                    if (WaitForSingleObject(pi.hProcess, 15000) == WAIT_TIMEOUT)
                                        TerminateProcess(pi.hProcess, 1);
                                    CloseHandle(pi.hThread);
                                    CloseHandle(pi.hProcess);
                                    Log(L"[ics] sharing enable command executed");
                                } else {
                                    Log(L"[ics] C++ ICS module failed to start");
                                }
                            }).detach();
                        } else {
                            Log(L"[ics] sharing off (auto-cleaned on disconnect too)");
                        }
                    }
                    return 0;
                }
                case (int)Ctl::SettingsBtn: {
                    // beta1.0.86: py-style dark settings window (cards:
                    // Language / Start at login / Auto-reconnect / Transfer cap)
                    HWND sw = CreateWindowExW(0, L"LhSettingsWindow",
                        TR(L"LinkHelm Settings", L"LinkHelm 设置"),
                        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
                        300, 120, 480, 560, hwnd, nullptr, g_app.inst, nullptr);
                    if (!sw) return 0;
                    // dark background via class brush; cards drawn as STATIC
                    // children with our card brush.
                    // beta1.0.91: flat page — no cards painted, only the
                    // base background (user: keep just the base color)
                    (void)0;
                    // Language card content
                    HWND l1 = CreateWindowW(L"STATIC",
                        TR(L"Language", L"语言"),
                        WS_CHILD | WS_VISIBLE, 26, 24, 300, 24, sw, nullptr,
                        g_app.inst, nullptr);
                    {
                        HFONT bf = CreateFontW(18, 0, 0, 0, FW_SEMIBOLD, 0, 0,
                                               0, DEFAULT_CHARSET, 0, 0,
                                               CLEARTYPE_QUALITY,
                                               DEFAULT_PITCH | FF_DONTCARE,
                                               L"Segoe UI");
                        SendMessageW(l1, WM_SETFONT, (WPARAM)bf, TRUE);
                    }
                    HWND hint1 = CreateWindowW(L"STATIC",
                        TR(L"Applies after a quick restart.",
                           L"界面语言，快速重启后生效。"),
                        WS_CHILD | WS_VISIBLE, 28, 52, 380, 20, sw, nullptr,
                        g_app.inst, nullptr);
                    HWND langEn = CreateWindowW(L"BUTTON", L"English",
                        WS_CHILD | WS_VISIBLE | BS_AUTORADIOBUTTON |
                            BS_OWNERDRAW | WS_GROUP,
                        28, 86, 110, 26, sw, (HMENU)(INT_PTR)20, g_app.inst,
                        nullptr);
                    HWND langZh = CreateWindowW(L"BUTTON", L"中文",
                        WS_CHILD | WS_VISIBLE | BS_AUTORADIOBUTTON |
                            BS_OWNERDRAW,
                        150, 86, 110, 26, sw, (HMENU)(INT_PTR)21, g_app.inst,
                        nullptr);
                    SendMessageW(g_lang_zh ? langZh : langEn, BM_SETCHECK,
                                 BST_CHECKED, 0);
                    // Start-at-login card
                    HWND hint2 = CreateWindowW(L"STATIC",
                        TR(L"Registry Run key (no admin needed).",
                           L"注册表 Run 键（无需管理员）。"),
                        WS_CHILD | WS_VISIBLE, 26, 208, 420, 20, sw, nullptr,
                        g_app.inst, nullptr);
                    HWND autoChk = CreateWindowW(L"STATIC",
                        TR(L"Launch LinkHelm when I log in",
                           L"登录时启动 LinkHelm"),
                        WS_CHILD | WS_VISIBLE | SS_NOTIFY | SS_OWNERDRAW,
                        26, 240, 420, 36, sw, (HMENU)(INT_PTR)Ctl::TglAutostart,
                        g_app.inst, nullptr);
                    HWND reconChk = CreateWindowW(L"STATIC",
                        TR(L"Auto-reconnect on link loss",
                           L"断线自动重连"),
                        WS_CHILD | WS_VISIBLE | SS_NOTIFY | SS_OWNERDRAW,
                        26, 352, 420, 36, sw,
                        (HMENU)(INT_PTR)Ctl::TglAutoconnect,
                        g_app.inst, nullptr);


                    // Transfer card
                    HWND l4 = CreateWindowW(L"STATIC",
                        TR(L"File transfer", L"文件传输"),
                        WS_CHILD | WS_VISIBLE, 28, 400, 300, 22, sw, nullptr,
                        g_app.inst, nullptr);
                    HWND hint4 = CreateWindowW(L"STATIC",
                        TR(L"Adaptive chunks (4MB for big files), 100 MB/s cap.",
                           L"自适应分块（大文件 4MB），上限 100 MB/s。"),
                        WS_CHILD | WS_VISIBLE, 26, 452, 430, 20, sw, nullptr,
                        g_app.inst, nullptr);
                    HWND closeBtn = CreateWindowW(L"BUTTON",
                        TR(L"Close", L"关闭"),
                        WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                        356, 508, 100, 32, sw, (HMENU)(INT_PTR)9, g_app.inst,
                        nullptr);
                    HFONT font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
                    for (HWND c : {l1, hint1, langEn, langZh, hint2,
                                   autoChk, reconChk, l4, hint4, closeBtn})
                        SendMessageW(c, WM_SETFONT, (WPARAM)font, TRUE);
                    ShowWindow(sw, SW_SHOW);
                    // set states AFTER visible: queue via posted message
                    struct ReflState { HWND autoChk; HWND reconChk; };
                    static ReflState rs{nullptr, nullptr};
                    rs.autoChk = autoChk;
                    rs.reconChk = reconChk;
                    PostMessageW(sw, WM_APP + 30, 0, (LPARAM)&rs);
                    return 0;
                }
                case (int)Ctl::AboutBtn:
                    MessageBoxW(hwnd,
                        g_lang_zh
                            ? L"【产品说明】\n"
                              L"LinkHelm Desktop 1.1.0（纯 C++ 版）。本软件通过"
                              L"手机热点把网络共享给电脑与下联设备使用，包含隧道连接、"
                              L"网络共享(ICS)、文件传输、商店加速四大功能。\n\n"
                              L"【开源组件】\n"
                              L"· mihomo（Clash Meta）— 商店加速核心，GPL-3.0 许可，"
                              L"源码：github.com/MetaCubeX/mihomo\n"
                              L"· hev-socks5-tunnel — lwIP 隧道引擎，MIT 许可\n"
                              L"· wintun — WireGuard 项目隧道驱动\n\n"
                              L"【隐私声明】\n"
                              L"本软件不收集、不上传任何用户数据；凭据仅保存在本机；"
                              L"网络流量仅经由你自己的手机出口转发。\n\n"
                              L"【使用合规】\n"
                              L"仅供个人学习与合法用途。使用者应遵守所在地法律法规，"
                              L"不得用于任何违法用途。软件按『现状』提供，不附带任何担保。"
                            : L"[Product]\n"
                              L"LinkHelm Desktop 1.1.0 (pure C++). Shares the "
                              L"phone's hotspot Internet with this PC and "
                              L"downstream devices: tunnel connect, network "
                              L"sharing (ICS), file transfer, store boost.\n\n"
                              L"[Open source]\n"
                              L"\u00b7 mihomo (Clash Meta) - store boost core, "
                              L"GPL-3.0, github.com/MetaCubeX/mihomo\n"
                              L"\u00b7 hev-socks5-tunnel - lwIP tunnel engine, MIT\n"
                              L"\u00b7 wintun - WireGuard tunnel driver\n\n"
                              L"[Privacy]\n"
                              L"No data collection, no uploads. Credentials stay "
                              L"on this machine; traffic exits via your own "
                              L"phone only.\n\n"
                              L"[Compliance]\n"
                              L"Personal and lawful use only; follow your local "
                              L"laws. Provided as-is, without warranty.",
                        TR(L"About", L"声明"), MB_OK | MB_ICONINFORMATION);
                    return 0;
                    return 0;
                case (int)Ctl::FtPairBtn: {
                    std::wstring ip = GetText(g_app.ft_pair_ip);
                    if (ip.empty()) {
                        UiText(g_app.ft_progress, TR(L"Enter the peer IP first", L"请先填写对方IP"));
                        return 0;
                    }
                    // real PAIR handshake (protocol: PAIR user pass -> PAIRED)
                    std::thread([ip] {
                        WSADATA w;
                        WSAStartup(MAKEWORD(2, 2), &w);
                        SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
                        sockaddr_in a{};
                        a.sin_family = AF_INET;
                        a.sin_port = htons(8765);
                        inet_pton(AF_INET, Utf8(ip).c_str(), &a.sin_addr);
                        if (connect(s, (sockaddr*)&a, sizeof(a)) != 0) {
                            closesocket(s);
                            Log(L"[ft] cannot reach " + ip);
                            UiText(g_app.ft_progress, TR(L"Cannot reach the peer", L"无法连接对方"));
                            return;
                        }
                        std::wstring uw2 = GetText(g_app.phone_user);
                        std::wstring pw2 = GetText(g_app.phone_pass);
                        if (uw2.empty()) uw2 = L"linkhelm01";
                        if (pw2.empty()) pw2 = L"k7h2m9x4qa";
                        std::string req = "PAIR " + Utf8(uw2) + " " +
                                          Utf8(pw2) + "\n";
                        send(s, req.c_str(), (int)req.size(), 0);
                        char buf[64]{};
                        u_long ms = 6000;
                        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO,
                                   (const char*)&ms, sizeof(ms));
                        int n = recv(s, buf, 63, 0);
                        closesocket(s);
                        if (n > 0 && strncmp(buf, "PAIRED", 6) == 0) {
                            Log(L"[ft] paired with " + ip);
                            UiText(g_app.ft_progress, TR(L"Paired. Pick a file, or Receive.",
                                   L"已配对。可发送或接收。"));
                        } else {
                            Log(L"[ft] pair refused");
                            UiText(g_app.ft_progress, TR(L"Peer did not confirm", L"对端未确认配对"));
                        }
                    }).detach();
                    return 0;
                }
                case (int)Ctl::FtDirBtn: {
                    // pick the save folder (kept for later receives)
                    wchar_t dir[MAX_PATH]{};
                    BROWSEINFOW bi{};
                    bi.hwndOwner = hwnd;
                    bi.lpszTitle = TR(L"Choose the folder for incoming files",
                                      L"选择接收文件保存目录");
                    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
                    PIDLIST_ABSOLUTE pidl = SHBrowseForFolderW(&bi);
                    if (pidl) {
                        if (SHGetPathFromIDListW(pidl, dir)) {
                            g_app.recv_dir = dir;
                            std::wstring t = TR(L"Save to: ", L"保存到：");
                            t += dir;
                            UiText(g_app.ft_dir_label, t.c_str());
                        }
                        CoTaskMemFree(pidl);
                    }
                    return 0;
                }
                case (int)Ctl::FtRecvBtn: {
                    if (g_app.recv_dir.empty()) {
                        UiText(g_app.ft_progress, TR(L"Choose a save folder first.",
                               L"请先选择保存目录。"));
                        return 0;
                    }
                    if (g_app.ft_rx) { g_app.ft_rx->Stop(); delete g_app.ft_rx; }
                    std::wstring uw3 = GetText(g_app.phone_user);
                    std::wstring pw3 = GetText(g_app.phone_pass);
                    if (uw3.empty()) uw3 = L"linkhelm01";
                    if (pw3.empty()) pw3 = L"k7h2m9x4qa";
                    g_app.ft_rx = new linkhelm::ReceiverServer(
                        "", Utf8(uw3), Utf8(pw3), g_app.recv_dir,
                        [](const std::wstring& m) { Log(L"[ft] " + m); },
                        [](const std::wstring& m) {
                            if (g_app.ft_progress)
                                UiText(g_app.ft_progress, m.c_str());
                        });
                    g_app.ft_rx->Start();
                    g_app.ft_rx->ArmOneFile();
                    UiText(g_app.ft_progress, TR(L"Waiting for the peer...",
                           L"等待对方发送…（完成后自动停止）"));
                    return 0;
                }
                case (int)Ctl::FtCancelBtn: {
                    if (g_app.ft_rx) { g_app.ft_rx->Stop(); delete g_app.ft_rx;
                                       g_app.ft_rx = nullptr; }
                    g_app.ft_busy.store(false);
                    UiText(g_app.ft_progress, TR(L"Cancelled.", L"已取消。"));
                    return 0;
                }
                case (int)Ctl::PasteBtn: {
                    if (OpenClipboard(hwnd)) {
                        HANDLE h = GetClipboardData(CF_UNICODETEXT);
                        if (h) {
                            UiText(g_app.sub_edit, (LPCWSTR)GlobalLock(h));
                            GlobalUnlock(h);
                        }
                        CloseClipboard();
                    }
                    return 0;
                }
                case (int)Ctl::StartBtn: OnStart(hwnd); return 0;
                case (int)Ctl::StopBtn: OnStop(hwnd); return 0;
                case (int)Ctl::DelayBtn:
                    SetStatus(TR(L"testing latency... (GitHub, 5s timeout)",
                        L"正在测速...（GitHub，5 秒超时）"));
                    std::thread(WorkerDelay, false).detach();
                    return 0;
                case (int)Ctl::AutoBtn:
                    SetStatus(TR(L"auto-selecting fastest node...",
                        L"正在自动选择最快节点..."));
                    std::thread(WorkerDelay, true).detach();
                    return 0;
                case (int)Ctl::RefreshBtn: RefreshNodes(); return 0;
                case (int)Ctl::HostsFixBtn:
                    if (linkhelm::RepairHostsBom()) {
                        UiText(g_app.hosts_label,
                               TR(L"hosts repaired (BOM removed)",
                                  L"hosts 已修复（移除 BOM）"));
                        Log(L"[accel] hosts: stray BOM characters removed");
                    }
                    return 0;
                case (int)Ctl::NodeCombo:
                    if (HIWORD(wp) == CBN_SELCHANGE) {
                        int idx = (int)SendMessageW(g_app.node_combo, CB_GETCURSEL, 0, 0);
                        if (idx >= 0 && idx < (int)g_app.node_names.size()) {
                            std::wstring node = g_app.node_names[idx];
                            std::thread([node]() {
                                g_app.accel.SelectNode(L"GLOBAL", node);
                            }).detach();
                            SetStatus(TR(L"node: ", L"当前节点: ") +
                                      NodeDisplayName(node));
                        }
                    }
                    return 0;
                default: return 0;
            }
        case WM_APP + 11: RefreshNodes(); return 0;
        case WM_APP + 12: RefreshNodes(); return 0;
        case WM_APP + 13: DrainLogQueue(); return 0;  // beta1.0.96 log drain
        case WM_CLOSE:
            // Tray residency: X minimizes, it never exits. Only the tray's
            // "quit & clean up network" ends the process.
            ShowWindow(hwnd, SW_HIDE);
            if (g_app.tray) {
                g_app.tray->ShowBalloon(L"LinkHelm",
                                        L"Still running in the tray - use the "
                                        L"tray menu to quit and clean up.");
            }
            return 0;
        case WM_DESTROY:
            if (g_app.tun) {
                g_app.tun->Stop();
                g_app.tun.reset();
            }
            g_app.accel.Stop();
            PostQuitMessage(0);
            return 0;
        case WM_DRAWITEM:
            if (DrawDarkCtl((DRAWITEMSTRUCT*)lp)) return TRUE;
            break;
        case WM_CTLCOLORSTATIC: {
            HDC hdc = (HDC)wp;
            SetTextColor(hdc, kText);
            SetBkColor(hdc, kBg);
            return (LRESULT)g_brBg;
        }

        default:
            return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

}  // namespace

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, PWSTR, int cmd_show) {
    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);
    InitCommonControls();

    g_app.inst = inst;
    g_brBg = CreateSolidBrush(kBg);
    g_brCard = CreateSolidBrush(kCard);
    g_brInput = CreateSolidBrush(kInput);
    // language: default English; lang.txt=zh switches to Chinese
    {
        wchar_t self0[MAX_PATH]{};
        GetModuleFileNameW(nullptr, self0, MAX_PATH);
        std::wstring lp(self0);
        auto sp0 = lp.rfind(L'\\');
        if (sp0 != std::wstring::npos) lp = lp.substr(0, sp0);
        lp += L"\\lang.txt";
        FILE* f = _wfopen(lp.c_str(), L"r");
        if (f) {
            wchar_t buf[8]{};
            fgetws(buf, 8, f);
            fclose(f);
            if (wcsstr(buf, L"zh")) g_lang_zh = true;
        }
    }
    // beta1.0.95 startup hygiene: a hard-killed previous session can leave
    // (a) an orphan hev engine holding the wintun adapter, (b) stale /1
    // routes, (c) the TUN adapter disabled. All three break the next connect.
    // Fire-and-forget; results land in the log.
    std::thread([] {
        int killed = 0;
        for (int i = 0; i < 20; ++i) {
            HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if (snap == INVALID_HANDLE_VALUE) break;
            PROCESSENTRY32W pe{};
            pe.dwSize = sizeof(pe);
            BOOL found = FALSE;
            if (Process32FirstW(snap, &pe)) {
                do {
                    if (_wcsicmp(pe.szExeFile, L"hev-socks5-tunnel.exe") == 0) {
                        HANDLE h = OpenProcess(PROCESS_TERMINATE |
                                               SYNCHRONIZE, FALSE,
                                               pe.th32ProcessID);
                        if (h) {
                            TerminateProcess(h, 1);
                            WaitForSingleObject(h, 3000);
                            CloseHandle(h);
                            killed++;
                        }
                        found = TRUE;
                        break;
                    }
                } while (Process32NextW(snap, &pe));
            }
            CloseHandle(snap);
            if (!found) break;
            Sleep(300);
        }
        if (killed) Log(L"[hygiene] removed " + std::to_wstring(killed) +
                        L" orphan engine(s) from a previous session");
        // stale /1 + phantom default routes
        int rc1 = (int)(intptr_t)ShellExecuteW(nullptr, L"open", L"cmd.exe",
            L"/c route delete 0.0.0.0 mask 128.0.0.0 10.0.0.1 >nul 2>&1"
            L" & route delete 128.0.0.0 mask 128.0.0.0 10.0.0.1 >nul 2>&1"
            L" & route delete 0.0.0.0 mask 0.0.0.0 10.0.0.1 >nul 2>&1",
            nullptr, SW_HIDE);
        (void)rc1;
        // re-enable a disabled TUN adapter (crashed sessions disable it)
        ShellExecuteW(nullptr, L"open", L"cmd.exe",
            L"/c netsh interface set interface \"LinkHelm-IwIP\" enable >nul 2>&1",
            nullptr, SW_HIDE);
    }).detach();

    wchar_t exe[MAX_PATH]{};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring exe_dir(exe);
    auto slash = exe_dir.rfind(L'\\');
    if (slash != std::wstring::npos) exe_dir = exe_dir.substr(0, slash);
    g_app.workdir = exe_dir + L"\\accel";
    CreateDirectoryW(g_app.workdir.c_str(), nullptr);
    g_app.mihomo_path = exe_dir + L"\\mihomo\\mihomo.exe";

    const wchar_t* kClass = L"LinkHelmDesktopWindow";
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.lpszClassName = kClass;
    wc.hbrBackground = g_brBg;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    // taskbar + titlebar icons: drawn brand icons (big + small variants)
    wc.hIcon = linkhelm::MakeBrandIcon();        // 64px alt-tab / taskbar
    wc.hIconSm = linkhelm::MakeBrandIconSmall(); // 16px titlebar corner
    RegisterClassExW(&wc);

    // beta1.0.86: pages MUST NOT be STATIC (a STATIC parent swallows mouse/
    // keyboard messages -> child edits become dead). Real registered class.
    WNDCLASSW pgc{};
    pgc.lpfnWndProc = PageProc;
    pgc.hInstance = inst;
    pgc.lpszClassName = L"LhPage";
    pgc.hbrBackground = g_brBg;
    pgc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    RegisterClassW(&pgc);

    // settings window class (own minimal proc; buttons post WM_CLOSE)
    WNDCLASSW swc{};
    swc.lpfnWndProc = SettingsProc;
    swc.hInstance = inst;
    swc.lpszClassName = L"LhSettingsWindow";
    swc.hbrBackground = g_brBg;
    swc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    RegisterClassW(&swc);

    HWND hwnd = CreateWindowExW(0, kClass,
        TR(L"LinkHelm Desktop 1.1.0 (pure C++)",
           L"LinkHelm 桌面版 1.1.0（纯C++）"),
                                WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU |
                                    WS_MINIMIZEBOX,
                                CW_USEDEFAULT, CW_USEDEFAULT, kWidth, kHeight,
                                nullptr, nullptr, inst, nullptr);
    if (!hwnd) return 1;
    g_app.hwnd = hwnd;

    // Tray: created on a dedicated thread-safe queue; the icon has its own
    // hidden window so it can never block the UI thread.
    static linkhelm::Tray tray(hwnd, inst, L"LinkHelm");
    g_app.tray = &tray;
    tray.SetLanguage(g_lang_zh);  // beta1.0.95: tray menu follows app language
    if (!tray.Install()) {
        Log(L"[tray] icon unavailable - quitting from the window is the only exit");
    }

    // beta1.0.86 (user directive): startup must NOT touch the PC's Wi-Fi
    // profiles, network connection names, or NCSI state. All ghost-network
    // sweeps and NCSI rewrites are disabled; only Clash-verge-style accel
    // keeps working. Cleanup happens exclusively on disconnect/exit for
    // resources WE created (tunnel adapter, routes).

    ShowWindow(hwnd, cmd_show);
    UpdateWindow(hwnd);
    // beta1.0.96: flush any log lines queued before the window existed.
    PostMessageW(hwnd, WM_APP + 13, 0, 0);

    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
        // Consume tray actions on the GUI thread only.
        if (g_app.tray) {
            switch (g_app.tray->Pump()) {
                case linkhelm::TrayAction::ShowWindow:
                    ShowWindow(hwnd, SW_SHOW);
                    SetForegroundWindow(hwnd);
                    break;
                case linkhelm::TrayAction::HideToTray:
                    ShowWindow(hwnd, SW_HIDE);
                    break;
                case linkhelm::TrayAction::Settings:
                    ShowWindow(hwnd, SW_SHOW);
                    SetForegroundWindow(hwnd);
                    break;
                case linkhelm::TrayAction::QuitAndCleanup:
                    if (g_app.tun) {
                        g_app.tun->Stop();
                        g_app.tun.reset();
                    }
                    g_app.accel.Stop();
                    g_app.tray->Remove();
                    DestroyWindow(hwnd);
                    break;
                default: break;
            }
        }
    }
    WSACleanup();
    return 0;
}
