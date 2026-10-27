// LinkHelm net operations module (beta1.0.84)
//
// C++ replacement for the Python netsh/route/ipconfig call sites in
// main.py (the Wintun adapter mechanism itself is untouched — only the
// CALLER moves to C++, per the settled architecture).
//
// Commands (argv[1..], all output UTF-8 to stdout, exit 0 on success):
//   SHOW_INTERFACES      netsh interface ipv4 show interfaces (GBK-safe)
//   IF_INDEX <name>      resolve adapter -> interface index
//   SET_ADDR <name> <ip/prefix> <gw>   netsh set address (idempotent)
//   SET_DNS <name> <dns1> [dns2..]     netsh set dns (static)
//   ADD_ROUTE <dest> <mask> <gw> <if>  route add with explicit IF + readback
//   DEL_ROUTE <dest> <mask> <gw>       route delete
//   ROUTE_GATEWAYS <dest> <mask>       parse `route print` -> gateway list
//   FLUSH_DNS            ipconfig /flushdns
//   DISABLE_IF <name>    netsh interface set interface disable
//
// All netsh/route output is read as ANSI (zh-CN = GBK) and re-emitted as
// UTF-8 — the same decoding discipline the Python side learned the hard way.

#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601

#include <windows.h>
#include <stdio.h>
#include <string>
#include <vector>

namespace {

std::string WideToUtf8(const wchar_t* w) {
    if (!w) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(size_t(n - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

std::string AnsiToUtf8(const char* a) {
    if (!a) return "";
    int w = MultiByteToWideChar(CP_ACP, 0, a, -1, nullptr, 0);
    if (w <= 0) return "";
    std::wstring ws(size_t(w - 1), L'\0');
    MultiByteToWideChar(CP_ACP, 0, a, -1, &ws[0], w);
    return WideToUtf8(ws.c_str());
}

// Run a console command, capture stdout as raw bytes (hidden window).
int RunCapture(const std::wstring& cmdline, std::string& out, DWORD timeoutMs = 20000) {
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    HANDLE rd = nullptr, wr = nullptr;
    CreatePipe(&rd, &wr, &sa, 0);
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{}; si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = wr; si.hStdError = wr; si.hStdInput = nullptr;
    PROCESS_INFORMATION pi{};
    std::wstring cmd = L"cmd.exe /c " + cmdline;
    BOOL ok = CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, TRUE,
                             CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
    CloseHandle(wr);
    if (!ok) { CloseHandle(rd); return -1; }
    out.clear();
    char buf[4096]; DWORD n = 0;
    while (ReadFile(rd, buf, sizeof(buf), &n, nullptr) && n) out.append(buf, n);
    CloseHandle(rd);
    WaitForSingleObject(pi.hProcess, timeoutMs);
    DWORD rc = 0;
    GetExitCodeProcess(pi.hProcess, &rc);
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return (int)rc;
}

int Run(const std::wstring& cmdline) {
    std::string out;
    return RunCapture(cmdline, out);
}

std::wstring Q(const std::wstring& s) { return L"\"" + s + L"\""; }  // quote

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) return 2;
    std::wstring cmd = argv[1];

    if (cmd == L"SHOW_INTERFACES") {
        std::string out;
        RunCapture(L"netsh interface ipv4 show interfaces", out);
        printf("%s", AnsiToUtf8(out.c_str()).c_str());
        return 0;
    }
    if (cmd == L"IF_INDEX" && argc == 3) {
        // netsh parsing: the Idx column of the row whose name column matches.
        std::string out;
        RunCapture(L"netsh interface ipv4 show interfaces", out);
        std::string utf8 = AnsiToUtf8(out.c_str());
        std::wstring needle = argv[2];
        std::string n = WideToUtf8(needle.c_str());
        // line format: Idx Met MTU State Name
        size_t pos = 0;
        while ((pos = utf8.find('\n', pos)) != std::string::npos) {
            size_t eol = utf8.find('\n', pos + 1);
            std::string line = utf8.substr(pos + 1, eol == std::string::npos
                                                  ? std::string::npos : eol - pos - 1);
            if (line.find(n) != std::string::npos) {
                // leading integer = index
                int idx = atoi(line.c_str());
                if (idx > 0) { printf("%d", idx); return 0; }
            }
            pos = eol == std::string::npos ? utf8.size() : eol;
        }
        return 1;
    }
    if (cmd == L"SET_ADDR" && argc == 5) {
        std::wstring name = argv[2], ip = argv[3], gw = argv[4];
        // static address; keep dhcp off (matches python _configure_adapter)
        return Run(L"netsh interface ipv4 set address name=" + Q(name) +
                   L" static " + ip + L" " + gw + L" 1");
    }
    if (cmd == L"SET_DNS" && argc >= 4) {
        std::wstring name = argv[2];
        std::wstring c = L"netsh interface ipv4 set dns name=" + Q(name) +
                         L" static " + argv[3] + L" primary validate=no";
        int rc = Run(c);
        for (int i = 4; i < argc; ++i) {
            Run(L"netsh interface ipv4 add dns name=" + Q(name) + L" " + argv[i] +
                L" index=" + std::to_wstring(i - 2) + L" validate=no");
        }
        return rc;
    }
    if (cmd == L"ADD_ROUTE" && argc == 6) {
        std::wstring dest = argv[2], mask = argv[3], gw = argv[4], idx = argv[5];
        // explicit IF binding + readback verification (the 1.0.72 lesson:
        // without IF, Windows may bind /1 routes to the WLAN).
        int rc = Run(L"route add " + dest + L" mask " + mask + L" " + gw +
                     L" IF " + idx + L" metric 1");
        if (rc != 0) return rc;
        // readback: `route print` match dest+mask -> verify the gateway line
        std::string out;
        RunCapture(L"route print -4", out);
        std::string utf8 = AnsiToUtf8(out.c_str());
        std::string d = WideToUtf8(dest.c_str()), g = WideToUtf8(gw.c_str());
        std::string m = WideToUtf8(mask.c_str());
        bool verified = false;
        // simpler robust check: find "dest mask" then the same line contains gw
        std::string pat = d.substr(0, d.size());
        size_t p = 0;
        while ((p = utf8.find(d, p)) != std::string::npos) {
            size_t lineStart = utf8.rfind('\n', p);
            lineStart = lineStart == std::string::npos ? 0 : lineStart + 1;
            size_t lineEnd = utf8.find('\n', p);
            std::string line = utf8.substr(lineStart, lineEnd - lineStart);
            if (line.find(m) != std::string::npos && line.find(g) != std::string::npos) {
                verified = true; break;
            }
            p += d.size();
        }
        printf("%s", verified ? "VERIFIED" : "UNVERIFIED");
        return verified ? 0 : 3;
    }
    if (cmd == L"DEL_ROUTE" && argc == 5) {
        std::wstring c1 = std::wstring(L"route delete ") + argv[2] + L" mask " + argv[3] + L" " + argv[4];
        return Run(c1);
    }
    if (cmd == L"ROUTE_GATEWAYS" && argc == 4) {
        std::string out;
        RunCapture(L"route print -4", out);
        std::string utf8 = AnsiToUtf8(out.c_str());
        std::string d = WideToUtf8(argv[2]), m = WideToUtf8(argv[3]);
        // strict: line's FIRST token == dest AND second == mask; gateway is
        // the 3rd token and must be a dotted IP (skip on-link/在链路上).
        std::vector<std::string> seen;
        size_t ls = 0;
        while (ls < utf8.size()) {
            size_t le = utf8.find('\n', ls);
            if (le == std::string::npos) le = utf8.size();
            std::string line = utf8.substr(ls, le - ls);
            // trim leading spaces
            size_t t = line.find_first_not_of(" \t");
            if (t != std::string::npos) {
                std::string trimmed = line.substr(t);
                if (trimmed.rfind(d + " ", 0) == 0) {
                    // tokenize
                    int tok = 0; size_t q = 0; std::string gw;
                    while (tok <= 2 && q < trimmed.size()) {
                        while (q < trimmed.size() && trimmed[q] == ' ') ++q;
                        size_t start = q;
                        while (q < trimmed.size() && trimmed[q] != ' ') ++q;
                        if (tok == 1 && trimmed.substr(start, q - start) != m) { break; }
                        if (tok == 2) gw = trimmed.substr(start, q - start);
                        ++tok;
                    }
                    bool isIp = !gw.empty() && gw.find('.') != std::string::npos
                                && gw.find_first_not_of("0123456789.") == std::string::npos;
                    bool dup = false;
                    for (auto& s : seen) if (s == gw) { dup = true; break; }
                    if (isIp && !dup) {
                        if (!seen.empty()) printf(" ");
                        printf("%s", gw.c_str());
                        seen.push_back(gw);
                    }
                }
            }
            ls = le + 1;
        }
        return 0;
    }
    if (cmd == L"FLUSH_DNS") {
        return Run(L"ipconfig /flushdns");
    }
    if (cmd == L"DISABLE_IF" && argc == 3) {
        return Run(L"netsh interface set interface " + Q(argv[2]) + L" disable");
    }
    return 2;
}
