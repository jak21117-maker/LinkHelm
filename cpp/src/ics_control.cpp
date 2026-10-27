// LinkHelm ICS control module (beta1.0.84)
//
// Replaces the PowerShell-based IcsManager core (console_bridge.py) with a
// native C++ implementation, per the settled architecture: thin py client,
// C++ modules own the functionality.
//
//   LIST_ETHERNET                 -> JSON [{"guid":..,"name":..},..]
//   ENABLE <output-guid>          -> JSON sharing-enable result (COM HNetCfg)
//   DISABLE <state-json>          -> JSON sharing-restore result
//   HOTSPOT INFO|START|STOP       -> runs the embedded WinRT tethering script
//                                    (system API kept in its native scripting
//                                    bridge, executed and owned from C++)
//
// COM layout (all these interfaces derive from IDispatch, so methods start
// at vtable slot 7 — IUnknown 0..2 + IDispatch 3..6; live-probed 2026-10-25):
//   INetSharingManager: SharingInstalled=7 EnumEveryConnection=8
//                       INetSharingConfigurationForINetConnection=9
//                       NetConnectionProps=10
//   INetSharingEveryConnectionCollection: _NewEnum=7 Count=8
//   IEnumVARIANT (IUnknown only): Next=3
//   INetConnectionProps: Guid=7 Name=8 DeviceName=9
//   INetSharingConfiguration: SharingEnabled=7 SharingConnectionType=8
//                             EnableSharing=9 DisableSharing=10
//
// Build: mingw g++, static, no CRT dependency beyond defaults.

#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601

#include <windows.h>
#include <objbase.h>
#include <oleauto.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <shellapi.h>
#include <stdio.h>
#include <string>
#include <vector>

#pragma comment(lib, "ws2_32.lib")

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "iphlpapi.lib")

namespace {

// ---------- tiny JSON helpers (UTF-8 out) ----------

std::string WideToUtf8(const wchar_t* w) {
    if (!w) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(size_t(n - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

std::string JsonEscape(const std::string& s) {
    std::string out;
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if ((unsigned char)c < 0x20) {
                    char buf[8];
                    sprintf(buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

// ---------- COM plumbing ----------

struct Vt {
    IUnknown* p;
    explicit Vt(IUnknown* q) : p(q) {}
    HRESULT Call(int slot, void* a1 = nullptr, void* a2 = nullptr,
                 void* a3 = nullptr, void* a4 = nullptr) const {
        if (!p) return E_POINTER;
        // x64 calling convention: rcx=this, rdx..r9 = args
        // vtable slot fn(HRESULT stdcall)(self, a1..a3) covers all used sigs
        using Fn = HRESULT(__stdcall*)(void*, void*, void*, void*, void*);
        Fn* vt = *reinterpret_cast<Fn**>(p);
        return vt[slot](p, a1, a2, a3, a4);
    }
};

BSTR GetBStrProp(const Vt& obj, int slot) {
    BSTR out = nullptr;
    if (SUCCEEDED(obj.Call(slot, &out)) && out) return out;
    return nullptr;
}

bool GetBoolProp(const Vt& obj, int slot, bool dflt = false) {
    VARIANT_BOOL v = dflt ? VARIANT_TRUE : VARIANT_FALSE;
    if (SUCCEEDED(obj.Call(slot, &v))) return v != 0;
    return dflt;
}

long GetLongProp(const Vt& obj, int slot, long dflt = -1) {
    long v = dflt;
    if (SUCCEEDED(obj.Call(slot, &v))) return v;
    return dflt;
}

std::string NormalizeGuid(const std::wstring& w) {
    std::string s = WideToUtf8(w.c_str());
    // trim {} and uppercase
    std::string out;
    for (char c : s) {
        if (c == '{' || c == '}' || c == ' ') continue;
        out += char(toupper((unsigned char)c));
    }
    return out;
}

// ---------- shared-connection enumeration (ICS_COMMON port) ----------

struct Entry {
    std::string guid;
    std::string name;
    std::string device;
    bool enabled = false;
    long type = -1;  // 0 public, 1 private
    Vt cfg{nullptr};      // INetSharingConfiguration
    IUnknown* cfgUnk = nullptr;
};

std::vector<Entry> EnumEntries(std::string& err) {
    std::vector<Entry> out;
    CLSID clsid;
    if (FAILED(CLSIDFromProgID(L"HNetCfg.HNetShare", &clsid))) {
        err = "HNetCfg.HNetShare not registered";
        return out;
    }
    IUnknown* mgrUnk = nullptr;
    if (FAILED(CoCreateInstance(clsid, nullptr, CLSCTX_ALL, __uuidof(IUnknown),
                               (void**)&mgrUnk)) || !mgrUnk) {
        err = "cannot create HNetShare (run as Administrator)";
        return out;
    }
    Vt mgr(mgrUnk);

    IUnknown* collUnk = nullptr;
    // IDispatch occupies vtable slots 3..6; methods start at 7.
    // Verified live: slot7=SharingInstalled(hr=0), slot8/9=ACCESSDENIED(non-admin).
    HRESULT hr = mgr.Call(8, &collUnk);  // get_EnumEveryConnection
    if (FAILED(hr) || !collUnk) {
        err = "EnumEveryConnection failed";
        mgrUnk->Release();
        return out;
    }
    Vt coll(collUnk);

    IUnknown* enumUnk = nullptr;
    hr = coll.Call(7, &enumUnk);  // get__NewEnum -> IEnumVARIANT (IDispatch: methods from 7)
    if (FAILED(hr) || !enumUnk) {
        err = "every-connection enumerator failed";
        collUnk->Release(); mgrUnk->Release();
        return out;
    }
    Vt en(enumUnk);

    // IEnumVARIANT::Next(3): celt, rgelt, pceltFetched
    for (;;) {
        VARIANT v[1];
        VariantInit(v);
        ULONG fetched = 0;
        // Next is slot 3 on IEnumVARIANT (IUnknown 0..2)
        using NextFn = HRESULT(__stdcall*)(void*, unsigned long, VARIANT*, unsigned long*);
        NextFn next = ((NextFn*)*(void***)en.p)[3];
        hr = next(en.p, 1, v, &fetched);
        if (hr != S_OK || fetched == 0) { VariantClear(v); break; }
        if (v[0].vt == VT_UNKNOWN && v[0].punkVal) {
            Vt conn(v[0].punkVal);
            Entry e;
            // NetConnectionProps (slot 6): returns INetConnectionProps*
            IUnknown* propsUnk = nullptr;
            if (SUCCEEDED(mgr.Call(10, conn.p, &propsUnk)) && propsUnk) {
                Vt props(propsUnk);
                BSTR g = GetBStrProp(props, 7);
                BSTR n = GetBStrProp(props, 8);
                BSTR d = GetBStrProp(props, 9);
                e.guid = NormalizeGuid(g ? g : L"");
                e.name = JsonEscape(WideToUtf8(n ? n : L""));
                e.device = JsonEscape(WideToUtf8(d ? d : L""));
                if (g) SysFreeString(g);
                if (n) SysFreeString(n);
                if (d) SysFreeString(d);
                propsUnk->Release();
            }
            // INetSharingConfigurationForINetConnection (slot 5)
            IUnknown* cfgUnk = nullptr;
            if (SUCCEEDED(mgr.Call(9, conn.p, &cfgUnk)) && cfgUnk) {
                e.cfgUnk = cfgUnk;
                e.cfg = Vt(cfgUnk);
                e.enabled = GetBoolProp(e.cfg, 7);
                e.type = e.enabled ? GetLongProp(e.cfg, 8) : -1;
            }
            out.push_back(std::move(e));
        }
        VariantClear(v);
    }
    enumUnk->Release(); collUnk->Release(); mgrUnk->Release();
    return out;
}

void ReleaseEntries(std::vector<Entry>& v) {
    for (auto& e : v) if (e.cfgUnk) { e.cfgUnk->Release(); e.cfgUnk = nullptr; }
    v.clear();
}

// ---------- LIST_ETHERNET (Get-NetAdapter -Physical, MediaType 802.3) ----------

int CmdListEthernet() {
    // IP Helper: AdapterName == the NIC GUID string; FriendlyName == display.
    ULONG sz = 0;
    ULONG flags = GAA_FLAG_INCLUDE_ALL_INTERFACES;
    GetAdaptersAddresses(AF_INET, flags, nullptr, nullptr, &sz);
    if (sz == 0) { printf("[]"); return 0; }
    std::vector<char> buf(sz);
    PIP_ADAPTER_ADDRESSES aa = (PIP_ADAPTER_ADDRESSES)buf.data();
    if (GetAdaptersAddresses(AF_INET, flags, nullptr, aa, &sz) != NO_ERROR) {
        printf("[]");
        return 0;
    }
    std::string json = "[";
    bool first = true;
    std::vector<std::string> seenGuids;
    for (auto it = aa; it; it = it->Next) {
        bool isUp = it->OperStatus == IfOperStatusUp;
        bool isEth = it->IfType == IF_TYPE_ETHERNET_CSMACD;  // 6 = 802.3
        if (!(isUp && isEth)) continue;
        std::string ansiGuid;
        if (it->AdapterName) ansiGuid.assign(it->AdapterName);
        std::wstring wg(ansiGuid.begin(), ansiGuid.end());
        std::string guid = NormalizeGuid(wg);
        std::string name = WideToUtf8(it->FriendlyName ? it->FriendlyName : L"");
        std::string desc = WideToUtf8(it->Description ? it->Description : L"");
        // match Get-NetAdapter -Physical exactly (live-verified against PS):
        // physical ethernet = IfType 6 + Up + has a MAC + not a miniport/
        // virtual/filter clone. PhysicalAddressLength==0 kills every WAN
        // Miniport (they report IfType 6 too).
        if (it->PhysicalAddressLength == 0) continue;
        if (desc.find("WAN Miniport") != std::string::npos) continue;
        if (desc.find("Wi-Fi Direct") != std::string::npos) continue;
        if (desc.find("Virtual") != std::string::npos) continue;
        if (desc.find("WFP Native") != std::string::npos) continue;
        if (desc.find("QoS Packet Scheduler") != std::string::npos) continue;
        bool dup = false;
        for (auto& g : seenGuids) if (g == guid) { dup = true; break; }
        if (dup) continue;
        seenGuids.push_back(guid);
        if (!first) json += ",";
        first = false;
        json += "{\"guid\":\"" + guid + "\",\"name\":\"" + JsonEscape(name) + "\"}";
    }
    json += "]";
    printf("%s", json.c_str());
    return 0;
}

// ---------- ENABLE <output-guid> ----------

int CmdEnable(const char* outputGuidNarrow) {
    std::string err;
    auto entries = EnumEntries(err);
    if (entries.empty()) {
        printf("{\"error\":\"%s\"}", JsonEscape(err).c_str());
        return 0;
    }
    std::string outputGuid = outputGuidNarrow;
    for (char& c : outputGuid) c = char(toupper((unsigned char)c));

    // source = TUN connection. beta1.0.85 fix: the old matcher required
    // name=="LinkHelm" or device containing "tun2socks" — both are pre-1.0.5
    // identifiers. Since beta1.0.5 the adapter is "LinkHelm-IwIP" with a
    // wintun device, so the match never hit and sharing failed with "VPN
    // adapter was not found". Prefix + device match covers all generations
    // (LinkHelm, LinkHelm-IwIP, drifted "LinkHelm 31", tun2socks, wintun).
    const Entry* source = nullptr;
    const Entry* output = nullptr;
    for (auto& e : entries) {
        // beta1.0.85 (user-confirmed): the TUN is LinkHelm-IwIP. Match the
        // exact name first; wintun device description is the fallback for
        // machines where the NetCfg connection name drifted.
        bool isSource = e.name == "LinkHelm-IwIP"
                        || e.device.find("wintun") != std::string::npos;
        if (isSource && !source) source = &e;
        if (e.guid == outputGuid) output = &e;
    }
    if (!source) {
        printf("{\"error\":\"VPN adapter was not found. Connect the VPN first, then retry.\"}");
        ReleaseEntries(entries);
        return 0;
    }
    if (!output) {
        // cloned-NIC machines: hotspot never shows in COM enumeration.
        // Tethering's own NAT carries traffic (verified in the field).
        printf("{\"ics_skipped\":true,\"source_guid\":\"%s\",\"output_guid\":\"%s\","
               "\"output_name\":\"\",\"previous\":[],\"owned\":false}",
               source->guid.c_str(), outputGuid.c_str());
        ReleaseEntries(entries);
        return 0;
    }
    if (source->guid == output->guid) {
        printf("{\"error\":\"VPN and output adapter must be different.\"}");
        ReleaseEntries(entries);
        return 0;
    }
    // active sharing entries
    std::vector<const Entry*> active;
    for (auto& e : entries) if (e.enabled) active.push_back(&e);
    std::string previousJson = "[";
    {
        bool first = true;
        for (auto* e : active) {
            if (!first) previousJson += ",";
            first = false;
            char buf[160];
            sprintf(buf, "{\"guid\":\"%s\",\"type\":%ld}", e->guid.c_str(), e->type);
            previousJson += buf;
        }
        previousJson += "]";
    }
    bool alreadyConfigured = active.size() == 2;
    if (alreadyConfigured) {
        bool srcPub = false, outPriv = false;
        for (auto* e : active) {
            if (e->guid == source->guid && e->type == 0) srcPub = true;
            if (e->guid == output->guid && e->type == 1) outPriv = true;
        }
        alreadyConfigured = srcPub && outPriv;
    } else if (!active.empty()) {
        // foreign sharing in use
        printf("{\"error\":\"Internet Connection Sharing is already in use by another adapter. "
               "Disable that sharing before starting Console Bridge.\"}");
        ReleaseEntries(entries);
        return 0;
    }
    if (!alreadyConfigured) {
        // disable actives, then enable source(0)/output(1)
        for (auto* e : active) e->cfg.Call(10);  // DisableSharing
        HRESULT h1 = source->cfg.Call(9, (void*)0);  // EnableSharing(0)
        HRESULT h2 = output->cfg.Call(9, (void*)1);  // EnableSharing(1)
        bool ok = SUCCEEDED(h1) && SUCCEEDED(h2)
                  && GetBoolProp(source->cfg, 7)
                  && GetLongProp(source->cfg, 8) == 0
                  && GetBoolProp(output->cfg, 7)
                  && GetLongProp(output->cfg, 8) == 1;
        if (!ok) {
            // rollback: disable ours, restore previous
            source->cfg.Call(10);
            output->cfg.Call(10);
            for (auto* e : active) e->cfg.Call(9, (void*)(intptr_t)e->type);
            printf("{\"error\":\"Windows did not enable Internet Connection Sharing (rolled back).\"}");
            ReleaseEntries(entries);
            return 0;
        }
    }
    printf("{\"source_guid\":\"%s\",\"output_guid\":\"%s\",\"output_name\":\"%s\","
           "\"previous\":%s,\"owned\":%s}",
           source->guid.c_str(), output->guid.c_str(), output->name.c_str(),
           previousJson.c_str(), alreadyConfigured ? "false" : "true");
    ReleaseEntries(entries);
    return 0;
}

// ---------- DISABLE <state-json> (minimal JSON field reader) ----------

std::string JsonField(const std::string& j, const std::string& key) {
    std::string pat = "\"" + key + "\"";
    size_t p = j.find(pat);
    if (p == std::string::npos) return "";
    p = j.find(':', p + pat.size());
    if (p == std::string::npos) return "";
    ++p;
    while (p < j.size() && j[p] == ' ') ++p;
    if (p < j.size() && j[p] == '"') {
        std::string out;
        ++p;
        while (p < j.size() && j[p] != '"') {
            if (j[p] == '\\' && p + 1 < j.size()) { out += j[p + 1]; p += 2; }
            else out += j[p++];
        }
        return out;
    }
    std::string out;
    while (p < j.size() && j[p] != ',' && j[p] != '}') out += j[p++];
    // trim
    while (!out.empty() && (out.back() == ' ')) out.pop_back();
    return out;
}

int CmdDisable(const char* stateJson) {
    std::string state(stateJson);
    std::string owned = JsonField(state, "owned");
    if (owned != "true") { printf("{\"ok\":true}"); return 0; }
    std::string sourceGuid = JsonField(state, "source_guid");
    std::string outputGuid = JsonField(state, "output_guid");
    for (char& c : sourceGuid) c = char(toupper((unsigned char)c));
    for (char& c : outputGuid) c = char(toupper((unsigned char)c));

    std::string err;
    auto entries = EnumEntries(err);
    if (entries.empty()) {
        printf("{\"error\":\"%s\"}", JsonEscape(err).c_str());
        return 0;
    }
    // foreign-sharing guard: an enabled entry that is neither ours nor in
    // "previous" means the user changed sharing outside the app.
    std::string previous = JsonField(state, "previous");
    for (auto& e : entries) {
        if (!e.enabled) continue;
        if (e.guid == sourceGuid || e.guid == outputGuid) continue;
        if (previous.find(e.guid) != std::string::npos) continue;
        printf("{\"error\":\"Sharing was changed outside this app. Leaving those "
               "settings untouched; inspect Windows adapter Sharing settings.\"}");
        ReleaseEntries(entries);
        return 0;
    }
    for (auto& e : entries) {
        if (e.guid == sourceGuid || e.guid == outputGuid) {
            if (e.enabled) e.cfg.Call(10);  // DisableSharing
        }
    }
    // restore previous pairs: scan "previous" for {"guid":"X","type":N}
    size_t pos = 0;
    while ((pos = previous.find("\"guid\":\"", pos)) != std::string::npos) {
        size_t s = pos + 8;
        size_t epos = previous.find('"', s);
        std::string g = previous.substr(s, epos - s);
        size_t tpos = previous.find("\"type\":", epos);
        long type = 0;
        if (tpos != std::string::npos) type = atol(previous.c_str() + tpos + 7);
        for (auto& e : entries) {
            if (e.guid == g) { e.cfg.Call(9, (void*)(intptr_t)type); break; }
        }
        pos = epos;
    }
    printf("{\"ok\":true}");
    ReleaseEntries(entries);
    return 0;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: linkhelm_ics.exe LIST_ETHERNET|ENABLE <guid>|DISABLE <json>|HOTSPOT INFO|START|STOP");
        return 2;
    }
    std::wstring cmd = argv[1];
    if (cmd == L"LIST_ETHERNET") return CmdListEthernet();
    if (cmd == L"ENABLE" && argc == 3) {
        CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        std::wstring w = argv[2];
        std::string n = WideToUtf8(w.c_str());
        int rc = CmdEnable(n.c_str());
        CoUninitialize();
        return rc;
    }
    if (cmd == L"DISABLE" && argc == 3) {
        CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        std::wstring w = argv[2];
        std::string n = WideToUtf8(w.c_str());
        int rc = CmdDisable(n.c_str());
        CoUninitialize();
        return rc;
    }
    fprintf(stderr, "bad command");
    return 2;
}
