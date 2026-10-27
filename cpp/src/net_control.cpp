// LinkHelm — Windows network control (see net_control.h).
#include "net_control.h"

#define _WIN32_WINNT 0x0601
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <iptypes.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <winreg.h>
// WIN32_LEAN_AND_MEAN hides COM — pull it in explicitly (beta1.0.71).
#include <objbase.h>
#include <oleauto.h>

#include <algorithm>
#include <cctype>
#include <cwctype>
#include <fstream>

#pragma comment(lib, "iphlpapi.lib")
#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "advapi32.lib")

namespace linkhelm {
namespace {

constexpr unsigned long kIfTypeTunnel = 131;         // IF_TYPE_TUNNEL
constexpr unsigned long kIfTypePropVirtual = 53;     // LinkHelm-IwIP
constexpr unsigned long kIfTypeWifi = 71;            // IF_TYPE_IEEE80211

std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n);
    return out;
}

// beta1.0.71 GBK fix: netsh on zh-CN Windows emits GBK (CP936), not UTF-8.
// Decoding it as UTF-8 silently produced mojibake and the parsers matched
// nothing. Try strict UTF-8 first; any invalid sequence falls back to the
// system ANSI code page — the same try/except ladder as python's
// _run_decoded (utf-8 -> mbcs) that was verified correct.
std::wstring Utf16Auto(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                                s.data(), (int)s.size(), nullptr, 0);
    if (n > 0) {
        std::wstring out((size_t)n, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                            out.data(), n);
        return out;
    }
    n = MultiByteToWideChar(CP_ACP, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out((size_t)(n > 0 ? n : 0), L'\0');
    if (n > 0) {
        MultiByteToWideChar(CP_ACP, 0, s.data(), (int)s.size(), out.data(), n);
    }
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

std::wstring Lower(std::wstring s) {
    // beta1.0.85 crash fix: std::towlower via static-libstdc++ + gc-sections
    // corrupted iteration state here (segv on the 2nd range-for element);
    // the Win32 char-mapping API is immune to that. Same semantics.
    if (!s.empty()) CharLowerBuffW(&s[0], (DWORD)s.size());
    return s;
}

// Collapses any drifted clone to its family root: "LinkHelm 31" -> "linkhelm",
// "DIRECT-LinkHelm-7" -> "direct-linkhelm".
std::wstring FamilyRoot(const std::wstring& name) {
    std::wstring low = Lower(name);
    if (low.rfind(L"direct-linkhelm", 0) == 0) return L"direct-linkhelm";
    if (low.rfind(L"linkhelm", 0) == 0) return L"linkhelm";
    return low;
}

bool StartsWithFamily(const std::wstring& name, const std::wstring& base) {
    std::wstring low = Lower(name);
    std::wstring b = Lower(base);
    return low.rfind(b, 0) == 0;
}

}  // namespace

bool RunCommand(const std::wstring& cmd, std::string* out) {
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE r = nullptr, w = nullptr;
    if (!CreatePipe(&r, &w, &sa, 0)) return false;
    SetHandleInformation(r, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    PROCESS_INFORMATION pi{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.hStdOutput = w;
    si.hStdError = w;
    si.wShowWindow = SW_HIDE;

    std::vector<wchar_t> cmdline(cmd.begin(), cmd.end());
    cmdline.push_back(L'\0');
    if (!CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(r);
        CloseHandle(w);
        return false;
    }
    CloseHandle(w);
    CloseHandle(pi.hThread);

    std::string buf;
    char tmp[4096];
    DWORD read = 0;
    while (ReadFile(r, tmp, sizeof(tmp), &read, nullptr) && read > 0) {
        buf.append(tmp, read);
    }
    CloseHandle(r);
    WaitForSingleObject(pi.hProcess, 15000);
    CloseHandle(pi.hProcess);
    if (out) *out = buf;
    return true;
}

std::vector<AdapterInfo> AdapterCensus() {
    std::vector<AdapterInfo> out;
    ULONG size = 0;
    if (GetAdaptersAddresses(AF_UNSPEC, 0, nullptr, nullptr, &size) !=
        ERROR_BUFFER_OVERFLOW) {
        return out;
    }
    std::vector<char> buf(size);
    auto* addrs = reinterpret_cast<IP_ADAPTER_ADDRESSES*>(buf.data());
    if (GetAdaptersAddresses(AF_UNSPEC, 0, nullptr, addrs, &size) != NO_ERROR) {
        return out;
    }
    for (auto* p = addrs; p != nullptr; p = p->Next) {
        AdapterInfo info;
        if (p->FriendlyName) info.name = p->FriendlyName;
        else if (p->Description) info.name = p->Description;
        info.type = p->IfType;
        info.is_tun = (p->IfType == kIfTypeTunnel || p->IfType == kIfTypePropVirtual);
        info.is_wifi = (p->IfType == kIfTypeWifi);
        out.push_back(info);
    }
    return out;
}

std::wstring CensusLine(const std::vector<AdapterInfo>& adapters) {
    std::wstring tuns, wifis;
    int tun_n = 0, wifi_n = 0;
    for (const auto& a : adapters) {
        if (a.is_tun) { ++tun_n; if (!tuns.empty()) tuns += L"/"; tuns += a.name; }
        if (a.is_wifi) { ++wifi_n; if (!wifis.empty()) wifis += L"/"; wifis += a.name; }
    }
    if (tuns.empty()) tuns = L"\u65e0";
    if (wifis.empty()) wifis = L"\u65e0";
    wchar_t buf[512];
    swprintf_s(buf, L"\u7f51\u5361\u666e\u67e5\uff1a\u5171 %zu \u4e2a\uff08TUN %d\uff1a%s\uff1bWi-Fi %d\uff1a%s\uff09",
               adapters.size(), tun_n, tuns.c_str(), wifi_n, wifis.c_str());
    return buf;
}

int SweepStaleWlanProfiles(const std::wstring& base_name) {
    std::string iface_out;
    RunCommand(L"netsh.exe wlan show interfaces", &iface_out);
    std::wstring iface = Utf16Auto(iface_out);

    // Guard 1: a LinkHelm network is live (or being auto-connected) -> skip
    // the whole sweep, never race the association.
    std::vector<std::wstring> active;
    {
        std::wstring line;
        for (size_t i = 0; i <= iface.size(); ++i) {
            if (i == iface.size() || iface[i] == L'\n') {
                auto pos = line.find(L':');
                if (pos != std::wstring::npos) {
                    std::wstring key = line.substr(0, pos);
                    std::wstring val = line.substr(pos + 1);
                    auto trim = [](std::wstring s) {
                        while (!s.empty() && (s.front() == L' ' || s.front() == L'\r')) s.erase(s.begin());
                        while (!s.empty() && (s.back() == L' ' || s.back() == L'\r')) s.pop_back();
                        return s;
                    };
                    key = Lower(trim(key));
                    val = trim(val);
                    if ((key == L"ssid" || key == L"profile" ||
                         key == L"\u914d\u7f6e\u6587\u4ef6") && !val.empty()) {
                        active.push_back(val);
                    }
                }
                line.clear();
            } else {
                line.push_back(iface[i]);
            }
        }
    }
    for (const auto& a : active) {
        if (StartsWithFamily(a, base_name) ||
            StartsWithFamily(a, L"direct-" + base_name)) {
            return 0;  // live LinkHelm link — do nothing
        }
    }

    std::string prof_out;
    RunCommand(L"netsh.exe wlan show profiles", &prof_out);
    std::wstring profiles = Utf16Auto(prof_out);

    std::vector<std::wstring> stale;
    std::wstring line;
    for (size_t i = 0; i <= profiles.size(); ++i) {
        if (i == profiles.size() || profiles[i] == L'\n') {
            auto pos = line.find(L':');
            if (pos != std::wstring::npos) {
                std::wstring name = line.substr(pos + 1);
                while (!name.empty() && (name.front() == L' ' || name.front() == L'\r')) name.erase(name.begin());
                while (!name.empty() && (name.back() == L' ' || name.back() == L'\r')) name.pop_back();
                std::wstring low = Lower(name);
                bool is_base = (low == Lower(base_name) || low == Lower(L"direct-" + base_name));
                // Keep the bare base names (auto-reconnect needs them); delete
                // every drifted clone in the family.
                if (!is_base && !name.empty() &&
                    (StartsWithFamily(name, base_name) ||
                     StartsWithFamily(name, L"direct-" + base_name))) {
                    stale.push_back(name);
                }
            }
            line.clear();
        } else {
            line.push_back(profiles[i]);
        }
    }

    // Guard 2: never delete an active profile, nor one whose family root is
    // currently in use.
    std::vector<std::wstring> live_roots;
    for (const auto& a : active) live_roots.push_back(FamilyRoot(a));
    int removed = 0;
    for (const auto& name : stale) {
        if (std::find(active.begin(), active.end(), name) != active.end()) continue;
        if (std::find(live_roots.begin(), live_roots.end(), FamilyRoot(name)) !=
            live_roots.end()) {
            continue;
        }
        std::wstring cmd = L"netsh.exe wlan delete profile name=\"" + name + L"\"";
        std::string dummy;
        if (RunCommand(cmd, &dummy)) ++removed;
    }
    return removed;
}

HostsReport InspectHosts() {
    HostsReport rep;
    wchar_t windir[MAX_PATH]{};
    GetWindowsDirectoryW(windir, MAX_PATH);
    rep.path = std::wstring(windir) + L"\\System32\\drivers\\etc\\hosts";
    std::ifstream fin(rep.path.c_str(), std::ios::binary);
    if (!fin) return rep;
    std::string raw((std::istreambuf_iterator<char>(fin)),
                    std::istreambuf_iterator<char>());
    rep.size = (long long)raw.size();
    // Count UTF-8 BOM sequences EF BB BF anywhere in the file.
    for (size_t i = 0; i + 2 < raw.size(); ++i) {
        if ((unsigned char)raw[i] == 0xEF && (unsigned char)raw[i + 1] == 0xBB &&
            (unsigned char)raw[i + 2] == 0xBF) {
            ++rep.bom_count;
        }
    }
    std::string line;
    for (char c : raw) {
        if (c == '\n') {
            while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
            if (!line.empty() && line[0] != '#') rep.active_entries.push_back(line);
            line.clear();
        } else {
            line.push_back(c);
        }
    }
    if (!line.empty() && line[0] != '#') rep.active_entries.push_back(line);
    return rep;
}

bool RepairHostsBom() {
    wchar_t windir[MAX_PATH]{};
    GetWindowsDirectoryW(windir, MAX_PATH);
    std::wstring path = std::wstring(windir) + L"\\System32\\drivers\\etc\\hosts";
    std::ifstream fin(path.c_str(), std::ios::binary);
    if (!fin) return false;
    std::string raw((std::istreambuf_iterator<char>(fin)),
                    std::istreambuf_iterator<char>());
    fin.close();
    std::string cleaned;
    cleaned.reserve(raw.size());
    for (size_t i = 0; i < raw.size();) {
        if (i + 2 < raw.size() && (unsigned char)raw[i] == 0xEF &&
            (unsigned char)raw[i + 1] == 0xBB && (unsigned char)raw[i + 2] == 0xBF) {
            i += 3;  // strip the BOM only — no entry is added/removed
            continue;
        }
        cleaned.push_back(raw[i++]);
    }
    if (cleaned == raw) return true;
    std::ofstream fout(path.c_str(), std::ios::binary | std::ios::trunc);
    if (!fout) return false;
    fout.write(cleaned.data(), (std::streamsize)cleaned.size());
    fout.close();
    return true;
}

bool ReadSystemProxy(bool* enabled, std::wstring* server) {
    HKEY key = nullptr;
    LONG rc = RegOpenKeyExW(
        HKEY_CURRENT_USER,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings",
        0, KEY_READ, &key);
    if (rc != ERROR_SUCCESS) return false;
    DWORD val = 0, type = 0, size = sizeof(val);
    if (RegQueryValueExW(key, L"ProxyEnable", nullptr, &type, (LPBYTE)&val,
                         &size) == ERROR_SUCCESS) {
        *enabled = val != 0;
    }
    wchar_t buf[512]{};
    size = sizeof(buf);
    if (RegQueryValueExW(key, L"ProxyServer", nullptr, &type, (LPBYTE)buf,
                         &size) == ERROR_SUCCESS) {
        *server = buf;
    }
    RegCloseKey(key);
    return true;
}

// ---------------------------------------------------------------------------
// beta1.0.71 — NLM COM, hand-declared. The vtable slot numbers below were
// verified LIVE from python (ctypes) on this machine before being ported:
// every interface here derives from IDispatch (7 slots), so the first custom
// method sits at slot 7. INetwork: GetName=7, SetName=8,
// get_IsConnectedToInternet=15, get_IsConnected=16. INetworkListManager:
// GetNetworks=7. IEnumNetworks: Next=8. INetwork has NO Delete method.
// ---------------------------------------------------------------------------

namespace {

const IID kCLSID_NetworkListManager = {
    0xDCB00C01, 0x570F, 0x4A9B,
    {0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}};
const IID kIID_INetworkListManager = {
    0xDCB00000, 0x570F, 0x4A9B,
    {0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}};
const IID kIID_IEnumNetworks = {
    0xDCB00003, 0x570F, 0x4A9B,
    {0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}};
const IID kIID_INetwork = {
    0xDCB00002, 0x570F, 0x4A9B,
    {0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}};

struct __declspec(novtable) LhDispatch : IUnknown {
    virtual HRESULT STDMETHODCALLTYPE GetTypeInfoCount(UINT*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetTypeInfo(UINT, LCID, IUnknown**) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetIDsOfNames(REFIID, LPOLESTR*, UINT,
                                                    LCID, DISPID*) = 0;
    virtual HRESULT STDMETHODCALLTYPE Invoke(DISPID, REFIID, LCID, WORD,
                                             DISPPARAMS*, VARIANT*,
                                             EXCEPINFO*, UINT*) = 0;
};

struct LhNetwork;

struct __declspec(novtable) LhEnumNetworks : LhDispatch {
    virtual HRESULT STDMETHODCALLTYPE get__NewEnum(IUnknown**) = 0;   // slot 7
    virtual HRESULT STDMETHODCALLTYPE Next(ULONG, LhNetwork**,  // slot 8
                                           ULONG*) = 0;
    virtual HRESULT STDMETHODCALLTYPE Skip(ULONG) = 0;
    virtual HRESULT STDMETHODCALLTYPE Reset() = 0;
    virtual HRESULT STDMETHODCALLTYPE Clone(LhEnumNetworks**) = 0;
};

struct __declspec(novtable) LhNetwork : LhDispatch {
    virtual HRESULT STDMETHODCALLTYPE GetName(BSTR*) = 0;             // 7
    virtual HRESULT STDMETHODCALLTYPE SetName(BSTR) = 0;              // 8
    virtual HRESULT STDMETHODCALLTYPE GetNetworkId(GUID*) = 0;        // 9
    virtual HRESULT STDMETHODCALLTYPE GetDescription(BSTR*) = 0;      // 10
    virtual HRESULT STDMETHODCALLTYPE SetDescription(BSTR) = 0;       // 11
    virtual HRESULT STDMETHODCALLTYPE GetDot11Ssid(void*) = 0;        // 12
    virtual HRESULT STDMETHODCALLTYPE GetCategory(DWORD*) = 0;        // 13
    virtual HRESULT STDMETHODCALLTYPE SetCategory(DWORD) = 0;         // 14
    virtual HRESULT STDMETHODCALLTYPE get_IsConnectedToInternet(      // 15
        VARIANT_BOOL*) = 0;
    virtual HRESULT STDMETHODCALLTYPE get_IsConnected(VARIANT_BOOL*) = 0;  // 16
    virtual HRESULT STDMETHODCALLTYPE GetConnectivity(DWORD*) = 0;    // 17
    virtual HRESULT STDMETHODCALLTYPE GetDomainType(DWORD*) = 0;      // 18
    virtual HRESULT STDMETHODCALLTYPE GetNetworkConnections(void**) = 0;  // 19
};

struct __declspec(novtable) LhNetworkListManager : LhDispatch {
    virtual HRESULT STDMETHODCALLTYPE GetNetworks(DWORD,     // slot 7
                                                  LhEnumNetworks**) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetNetwork(GUID, LhNetwork**) = 0;
};

}  // namespace

namespace {

bool NameIsGhostFamily(const std::wstring& name) {
    std::wstring low = Lower(name);
    if (low == L"direct-linkhelm") return false;  // phone network: untouchable
    if (low == L"linkhelm") return true;
    return low.rfind(L"linkhelm ", 0) == 0;  // "LinkHelm 2".."LinkHelm 37"
}

}  // namespace

int SweepGhostNetworks(
    const std::function<void(const std::wstring&)>& log_fn) {
    auto log = [&log_fn](const std::wstring& m) {
        if (log_fn) log_fn(m);
    };
    if (FAILED(CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED))) return 0;

    // ---- pass 1: enumerate — who is connected, who is a dead ghost? ------
    int renamed = 0, deleted = 0;
    std::vector<std::wstring> ghosts;
    std::wstring live_drift;
    LhNetworkListManager* nlm = nullptr;
    LhEnumNetworks* en = nullptr;
    std::vector<LhNetwork*> live_nets;  // kept for pass 2 (rename)
    HRESULT hr = CoCreateInstance(kCLSID_NetworkListManager, nullptr,
                                  CLSCTX_INPROC_SERVER, kIID_INetworkListManager,
                                  (void**)&nlm);
    if (FAILED(hr)) {
        log(L"[netlist] CoCreateInstance failed 0x" +
            std::to_wstring((unsigned)hr) + L" — ghost entries untouched");
        CoUninitialize();
        return 0;
    }
    if (SUCCEEDED(nlm->GetNetworks(3 /* NLM_ENUM_NETWORKS_ALL */, &en)) && en) {
        for (;;) {
            LhNetwork* net = nullptr;
            ULONG got = 0;
            if (FAILED(en->Next(1, &net, &got)) || got == 0) break;
            BSTR name = nullptr;
            VARIANT_BOOL con = VARIANT_FALSE, con_inet = VARIANT_FALSE;
            if (SUCCEEDED(net->GetName(&name)) && name) {
                net->get_IsConnected(&con);
                net->get_IsConnectedToInternet(&con_inet);
                std::wstring n(name, SysStringLen(name));
                SysFreeString(name);
                if (NameIsGhostFamily(n)) {
                    if (con == VARIANT_TRUE) {
                        if (Lower(n) != L"linkhelm") live_drift = n;
                        live_nets.push_back(net);  // pass 2 target
                        continue;                  // keep the ref
                    }
                    ghosts.push_back(n);
                }
            }
            net->Release();
        }
        en->Release();
    } else {
        log(L"[netlist] GetNetworks failed — ghost entries untouched");
    }

    // ---- pass 2: rename the live drift (direct vtable call, slot 8) ------
    if (!live_drift.empty()) {
        for (LhNetwork* net : live_nets) {
            BSTR name = nullptr;
            if (FAILED(net->GetName(&name)) || !name) continue;
            std::wstring n(name, SysStringLen(name));
            SysFreeString(name);
            if (n != live_drift) continue;
            BSTR new_name = SysAllocString(L"LinkHelm");
            HRESULT rhr = net->SetName(new_name);
            if (new_name) SysFreeString(new_name);
            if (SUCCEEDED(rhr)) {
                renamed = 1;
                log(L"[netlist] renamed \"" + live_drift +
                    L"\" -> \"LinkHelm\" (fixed name, no more drift)");
            } else if (rhr == DISP_E_EXCEPTION || rhr == E_ACCESSDENIED) {
                log(L"[netlist] rename of \"" + live_drift +
                    L"\" rejected by Windows — needs administrator rights");
            } else {
                log(L"[netlist] rename failed 0x" +
                    std::to_wstring((unsigned)rhr));
            }
            break;
        }
    }
    for (LhNetwork* net : live_nets) net->Release();
    nlm->Release();
    CoUninitialize();

    // ---- pass 3: delete disconnected ghosts from the registry store ------
    if (!ghosts.empty()) {
        const std::wstring base =
            L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\NetworkList";
        std::function<int(HKEY, const std::wstring&)> purge =
            [&](HKEY root, const std::wstring& path) -> int {
            int n = 0;
            HKEY key = nullptr;
            if (RegOpenKeyExW(root, path.c_str(), 0,
                              KEY_READ | KEY_SET_VALUE,
                              &key) != ERROR_SUCCESS) {
                log(L"[netlist] " + path +
                    L" store locked — needs administrator rights");
                return 0;
            }
            DWORD max_len = 256;
            RegQueryInfoKeyW(key, nullptr, nullptr, nullptr, nullptr, &max_len,
                             nullptr, nullptr, nullptr, nullptr, nullptr,
                             nullptr);
            std::vector<std::wstring> subs;
            std::vector<wchar_t> name(max_len + 1, L'\0');
            for (DWORD i = 0;; ++i) {
                DWORD len = max_len + 1;
                if (RegEnumKeyExW(key, i, name.data(), &len, nullptr, nullptr,
                                  nullptr, nullptr) != ERROR_SUCCESS) {
                    break;
                }
                subs.emplace_back(name.data(), len);
            }
            for (const auto& sub : subs) {
                std::wstring sub_path = path + L"\\" + sub;
                HKEY sk = nullptr;
                if (RegOpenKeyExW(key, sub.c_str(), 0, KEY_READ, &sk) !=
                    ERROR_SUCCESS) {
                    continue;
                }
                wchar_t val[512]{};
                DWORD vtype = 0, vsize = sizeof(val);
                std::wstring name_val;
                for (const wchar_t* vn : {L"ProfileName", L"Name"}) {
                    vsize = sizeof(val);
                    if (RegQueryValueExW(sk, vn, nullptr, &vtype, (LPBYTE)val,
                                         &vsize) == ERROR_SUCCESS &&
                        vtype == REG_SZ) {
                        name_val.assign(val);
                        break;
                    }
                }
                RegCloseKey(sk);
                std::wstring low = Lower(name_val);
                bool hit = false;
                for (const auto& g : ghosts) {
                    if (low == Lower(g)) { hit = true; break; }
                }
                if (hit) {
                    if (RegDeleteTreeW(key, sub.c_str()) == ERROR_SUCCESS) {
                        ++n;
                        continue;
                    }
                }
                n += purge(root, sub_path);
            }
            RegCloseKey(key);
            return n;
        };
        deleted += purge(HKEY_LOCAL_MACHINE, base + L"\\Profiles");
        deleted += purge(HKEY_LOCAL_MACHINE, base + L"\\Signatures");
        if (deleted > 0) {
            log(L"[netlist] removed " + std::to_wstring(deleted) +
                L" ghost network(s)");
        } else {
            log(L"[netlist] " + std::to_wstring(ghosts.size()) +
                L" ghost(s) found but none removable — needs administrator "
                L"rights");
        }
    }
    (void)renamed;
    return deleted;
}

namespace {

// NCSI probe registry values. beta1.0.82: healed to the MICROSOFT DEFAULTS
// and left there for the whole session — the old Baidu redirect (1.0.71..
// 1.0.81) was disproven live: with the tunnel up the Microsoft endpoints
// answer through the phone's upstream, and the flyout only showed "Internet"
// again once the values went back to Microsoft defaults. Nothing is swapped
// per-session anymore, so RestoreNcsiProbe() is a no-op kept for callers.
const wchar_t* kNcsiKeyPath =
    L"SYSTEM\\CurrentControlSet\\Services\\NlaSvc\\Parameters\\Internet";
const wchar_t* kNcsiValues[7] = {
    L"ActiveWebProbeHost", L"ActiveWebProbePath", L"ActiveWebProbeContent",
    L"ActiveWebProbeContentMatch",
    L"ActiveWebProbeHostV6", L"ActiveWebProbePathV6",
    L"ActiveWebProbeContentV6"};
const wchar_t* kNcsiDomestic[7] = {
    L"www.msftconnecttest.com", L"/connecttest.txt",
    L"Microsoft Connect Test", L"Microsoft Connect Test",
    L"ipv6.msftconnecttest.com", L"/connecttest.txt",
    L"Microsoft Connect Test"};

struct NcsiBackup {
    std::wstring value;
    bool present = false;
};
NcsiBackup g_ncsi_backup[7];

}  // namespace

bool SetDomesticNcsiProbe(
    const std::function<void(const std::wstring&)>& log_fn) {
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kNcsiKeyPath, 0,
                      KEY_READ | KEY_SET_VALUE,
                      &key) != ERROR_SUCCESS) {
        if (log_fn) {
            log_fn(L"[ncsi] probe values left untouched (needs "
                   L"administrator rights)");
        }
        return false;
    }
    // beta1.0.82: heal to Microsoft defaults — only write when a value
    // differs (leftover Baidu redirect from 1.0.71..1.0.81 gets cleaned).
    bool changed = false;
    for (int i = 0; i < 7; ++i) {
        wchar_t buf[512]{};
        DWORD vtype = 0, size = sizeof(buf);
        bool matches =
            RegQueryValueExW(key, kNcsiValues[i], nullptr, &vtype,
                             (LPBYTE)buf, &size) == ERROR_SUCCESS &&
            vtype == REG_SZ && wcscmp(buf, kNcsiDomestic[i]) == 0;
        if (!matches) {
            RegSetValueExW(key, kNcsiValues[i], 0, REG_SZ,
                           (const BYTE*)kNcsiDomestic[i],
                           (DWORD)((wcslen(kNcsiDomestic[i]) + 1) *
                                   sizeof(wchar_t)));
            changed = true;
        }
        g_ncsi_backup[i].present = false;  // nothing session-specific
    }
    RegCloseKey(key);
    if (log_fn) {
        log_fn(changed
                   ? L"[ncsi] probe values healed to Microsoft defaults"
                   : L"[ncsi] probe values already at Microsoft defaults");
    }
    if (changed) RestartNlasvc(log_fn);
    return true;
}

void RestartNlasvc(const std::function<void(const std::wstring&)>& log_fn) {
    const wchar_t* cmds[2] = {L"net.exe stop nlasvc /y", L"net.exe start nlasvc"};
    for (int i = 0; i < 2; ++i) {
        STARTUPINFOW si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESHOWWINDOW;
        si.wShowWindow = SW_HIDE;
        PROCESS_INFORMATION pi{};
        wchar_t cmdline[128]{};
        lstrcpynW(cmdline, cmds[i], 127);
        if (CreateProcessW(nullptr, cmdline, nullptr, nullptr, FALSE,
                           CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
            WaitForSingleObject(pi.hProcess, 30000);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            if (log_fn) log_fn(L"[ncsi] ran: " + std::wstring(cmds[i]));
        } else if (log_fn) {
            log_fn(L"[ncsi] failed to run: " + std::wstring(cmds[i]));
        }
    }
}

bool RestoreNcsiProbe(
    const std::function<void(const std::wstring&)>& log_fn) {
    // beta1.0.82: no-op — probe values stay at Microsoft defaults for the
    // whole session (kept so existing exit paths don't need rewriting).
    (void)log_fn;
    return false;
}

// ---------------------------------------------------------------------------
// beta1.0.8 — EnableActiveProbing heal (the flyout display bug, part 2).
// A 0 here is almost never a user choice: it is the leftover of the old
// beta1.0.55 session pause, and it survives crashes forever. With probing
// off, NLA validates nothing and every network — including a working
// tunnel — is labelled "无法访问 Internet". Snapshot -> force ON -> restore.
// ---------------------------------------------------------------------------

namespace {
int g_ncsi_probing_original = -1;  // -1 = unknown / leave as found
}

void RememberNcsiOriginal() {
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kNcsiKeyPath, 0, KEY_READ, &key) !=
        ERROR_SUCCESS) {
        return;
    }
    DWORD val = 0, type = 0, size = sizeof(val);
    if (RegQueryValueExW(key, L"EnableActiveProbing", nullptr, &type,
                         (LPBYTE)&val, &size) == ERROR_SUCCESS &&
        type == REG_DWORD) {
        g_ncsi_probing_original = val ? 1 : 0;
    } else {
        g_ncsi_probing_original = 1;  // Windows default when never written
    }
    RegCloseKey(key);
}

bool ForceNcsiProbeOn(
    const std::function<void(const std::wstring&)>& log_fn) {
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kNcsiKeyPath, 0,
                      KEY_READ | KEY_SET_VALUE, &key) != ERROR_SUCCESS) {
        if (log_fn) {
            log_fn(L"[ncsi] cannot heal EnableActiveProbing (needs "
                   L"administrator rights)");
        }
        return false;
    }
    DWORD on = 1;
    LONG rc = RegSetValueExW(key, L"EnableActiveProbing", 0, REG_DWORD,
                             (const BYTE*)&on, sizeof(on));
    RegCloseKey(key);
    if (rc == ERROR_SUCCESS && log_fn) {
        log_fn(L"[ncsi] connectivity probing forced ON (heals the stale "
               L"disabled state); original restored at exit");
    }
    return rc == ERROR_SUCCESS;
}

bool RestoreNcsiOriginal(
    const std::function<void(const std::wstring&)>& log_fn) {
    if (g_ncsi_probing_original < 0) return false;
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kNcsiKeyPath, 0, KEY_SET_VALUE,
                      &key) != ERROR_SUCCESS) {
        return false;
    }
    DWORD val = (DWORD)g_ncsi_probing_original;
    RegSetValueExW(key, L"EnableActiveProbing", 0, REG_DWORD,
                   (const BYTE*)&val, sizeof(val));
    RegCloseKey(key);
    g_ncsi_probing_original = -1;
    if (log_fn) log_fn(L"[ncsi] original probing value restored");
    return true;
}

}  // namespace linkhelm
