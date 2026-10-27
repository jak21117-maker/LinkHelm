// LinkHelm — store-accelerator controller (see accel_controller.h).
// Uses WinHTTP against 127.0.0.1 only: the micro-kernel is loopback-only by
// design, so it coexists with Clash Verge instead of stealing its proxy.
#include "accel_controller.h"

#include <winhttp.h>

#include <algorithm>
#include <sstream>
#include <thread>

#ifndef WINHTTP_NO_HEADER_DATA
#define WINHTTP_NO_HEADER_DATA NULL
#endif
#pragma comment(lib, "winhttp.lib")

namespace linkhelm {
namespace {

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                        out.data(), n, nullptr, nullptr);
    return out;
}

std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                                nullptr, 0);
    std::wstring out(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                        out.data(), n);
    return out;
}

std::wstring PercentEncode(const std::wstring& in) {
    static const wchar_t* kUnreserved = L"-_.~";
    if (in.empty()) return {};
    // beta1.0.99 DELAY-TEST ROOT CAUSE: the old loop cast wchar_t to
    // unsigned char, keeping only the LOW byte — "香" (U+9999) became
    // "%99" and flag emoji became garbage. mihomo answered 404 for every
    // node whose name has CJK/emoji, so the latency test NEVER worked.
    // Correct: convert to UTF-8 first, then percent-encode each byte.
    int n = WideCharToMultiByte(CP_UTF8, 0, in.data(), (int)in.size(),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string u((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, in.data(), (int)in.size(), u.data(), n,
                        nullptr, nullptr);
    std::wstring out;
    for (unsigned char b : u) {
        if (std::isalnum(b) ||
            (b < 128 && wcschr(kUnreserved, (wchar_t)b))) {
            out.push_back((wchar_t)b);
        } else {
            wchar_t buf[8];
            swprintf_s(buf, L"%%%02X", b);
            out += buf;
        }
    }
    return out;
}

// {"delay":1234} -> 1234 ; anything else -> -1
long long ParseDelay(const std::string& body) {
    auto pos = body.find("\"delay\"");
    if (pos == std::string::npos) return -1;
    pos = body.find(':', pos);
    if (pos == std::string::npos) return -1;
    ++pos;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '"')) ++pos;
    // beta1.0.8: strtol instead of stoll — the build compiles with
    // -fno-exceptions, and stoll throws on garbage input (it would abort).
    std::string digits = body.substr(pos);
    char* end = nullptr;
    long long v = std::strtoll(digits.c_str(), &end, 10);
    if (end == digits.c_str()) return -1;
    return v;
}

}  // namespace

bool AccelController::Start(const std::wstring& mihomo_path,
                            const std::wstring& workdir,
                            const std::wstring& config_path,
                            int mixed_port, int api_port,
                            std::wstring* error) {
    Stop();
    std::wstring cmd = L"\"" + mihomo_path + L"\" -d \"" + workdir +
                       L"\" -f \"" + config_path + L"\"";
    STARTUPINFOW si{};
    PROCESS_INFORMATION pi{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    std::vector<wchar_t> cmdline(cmd.begin(), cmd.end());
    cmdline.push_back(L'\0');
    if (!CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, workdir.c_str(), &si, &pi)) {
        if (error) *error = L"failed to launch mihomo";
        return false;
    }
    CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    api_port_ = api_port;
    mixed_port_ = mixed_port;
    return true;
}

void AccelController::Stop() {
    if (!process_) return;
    TerminateProcess(process_, 0);
    WaitForSingleObject(process_, 3000);
    CloseHandle(process_);
    process_ = nullptr;
}

bool AccelController::IsRunning() const {
    if (!process_) return false;
    DWORD code = 0;
    if (!GetExitCodeProcess(process_, &code)) return false;
    return code == STILL_ACTIVE;
}

std::wstring AccelController::HttpGet(const std::wstring& path,
                                      int timeout_ms, bool* ok) {
    *ok = false;
    std::wstring host = L"127.0.0.1";
    HINTERNET sess = WinHttpOpen(L"LinkHelm/1.0",
                                 WINHTTP_ACCESS_TYPE_NO_PROXY,
                                 WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!sess) return {};
    HINTERNET conn = WinHttpConnect(sess, host.c_str(), (INTERNET_PORT)api_port_, 0);
    if (!conn) { WinHttpCloseHandle(sess); return {}; }
    HINTERNET req = WinHttpOpenRequest(conn, L"GET", path.c_str(), nullptr,
                                       WINHTTP_NO_REFERER,
                                       WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!req) { WinHttpCloseHandle(conn); WinHttpCloseHandle(sess); return {}; }
    WinHttpSetTimeouts(req, timeout_ms, timeout_ms, timeout_ms, timeout_ms);

    std::string body;
    if (WinHttpSendRequest(req, WINHTTP_NO_HEADER_DATA, 0,
                           WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
        WinHttpReceiveResponse(req, nullptr)) {
        DWORD avail = 0;
        while (WinHttpQueryDataAvailable(req, &avail) && avail > 0) {
            std::string chunk(avail, '\0');
            DWORD read = 0;
            if (WinHttpReadData(req, chunk.data(), avail, &read) && read > 0) {
                chunk.resize(read);
                body += chunk;
            }
        }
        *ok = true;
    }
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(conn);
    WinHttpCloseHandle(sess);
    return Utf16(body);
}

long long AccelController::TestDelay(const std::wstring& node, int timeout_ms,
                                     const std::wstring& url) {
    std::wstring path = L"/proxies/" + PercentEncode(node) +
                        L"/delay?timeout=" + std::to_wstring(timeout_ms) +
                        L"&url=" + PercentEncode(url);
    bool ok = false;
    std::wstring body = HttpGet(path, timeout_ms + 5000, &ok);
    if (!ok) return -1;
    return ParseDelay(Utf8(body));
}

std::vector<DelayResult> AccelController::TestAllDelays(
    const std::vector<std::wstring>& nodes, int timeout_ms,
    const std::wstring& url) {
    // beta1.0.96 CRASH FIX: the old pool spawned ONE THREAD PER NODE — a
    // subscription with hundreds of entries meant hundreds of simultaneous
    // WinHTTP threads, which froze and could kill the whole UI ("商店加速
    // 干崩 UI"). Cap concurrency at 8 and run the rest in waves; the caller
    // is already a worker thread, so the message pump never blocks.
    std::vector<DelayResult> results(nodes.size());
    for (size_t i = 0; i < nodes.size(); ++i) results[i].node = nodes[i];
    const size_t kBatch = 8;  // user directive: 8 per batch
    for (size_t base = 0; base < nodes.size(); base += kBatch) {
        size_t end = base + kBatch;
        if (end > nodes.size()) end = nodes.size();
        std::vector<std::thread> pool;
        pool.reserve(end - base);
        for (size_t i = base; i < end; ++i) {
            pool.emplace_back([this, i, &results, &nodes, timeout_ms, &url]() {
                results[i].ms = TestDelay(nodes[i], timeout_ms, url);
            });
        }
        for (auto& t : pool) if (t.joinable()) t.join();
    }
    std::sort(results.begin(), results.end(),
              [](const DelayResult& a, const DelayResult& b) {
                  long long av = a.ms < 0 ? LLONG_MAX : a.ms;
                  long long bv = b.ms < 0 ? LLONG_MAX : b.ms;
                  return av < bv;
              });
    return results;
}

bool AccelController::HttpPut(const std::wstring& path,
                              const std::wstring& body, int timeout_ms) {
    std::wstring host = L"127.0.0.1";
    HINTERNET sess = WinHttpOpen(L"LinkHelm/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                 WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!sess) return false;
    HINTERNET conn = WinHttpConnect(sess, host.c_str(), (INTERNET_PORT)api_port_, 0);
    if (!conn) { WinHttpCloseHandle(sess); return false; }
    HINTERNET req = WinHttpOpenRequest(conn, L"PUT", path.c_str(), nullptr,
                                       WINHTTP_NO_REFERER,
                                       WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!req) { WinHttpCloseHandle(conn); WinHttpCloseHandle(sess); return false; }
    WinHttpSetTimeouts(req, timeout_ms, timeout_ms, timeout_ms, timeout_ms);
    std::string utf8 = Utf8(body);
    LPCWSTR hdr = L"Content-Type: application/json";
    bool ok = WinHttpSendRequest(req, hdr, (DWORD)-1,
                                 (LPVOID)utf8.data(), (DWORD)utf8.size(),
                                 (DWORD)utf8.size(), 0) &&
              WinHttpReceiveResponse(req, nullptr);
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(conn);
    WinHttpCloseHandle(sess);
    return ok;
}

bool AccelController::SelectNode(const std::wstring& group,
                                 const std::wstring& node) {
    std::wstring path = L"/proxies/" + PercentEncode(group);
    std::wstring body = L"{\"name\":\"" + node + L"\"}";
    return HttpPut(path, body, 3000);
}

}  // namespace linkhelm
