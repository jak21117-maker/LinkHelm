// LinkHelm — store-accelerator controller: mihomo process + latency tests.
// C++ rewrite P2 core. In Python these were subprocess + urllib calls that
// could block the single Tk thread; here every call runs on the caller's
// worker thread and the results come back as plain data.
#pragma once

#include <windows.h>

#include <map>
#include <string>
#include <vector>

namespace linkhelm {

struct DelayResult {
    std::wstring node;
    long long ms = -1;   // -1 == timeout / unreachable
};

class AccelController {
public:
    // Launch "mihomo.exe -d <workdir> -f <config>" with a hidden console.
    // Returns true and fills apiPort when the loopback API answers.
    bool Start(const std::wstring& mihomo_path,
               const std::wstring& workdir,
               const std::wstring& config_path,
               int mixed_port,
               int api_port,
               std::wstring* error);
    void Stop();
    bool IsRunning() const;

    // GET /proxies/<node>/delay?timeout=<ms>&url=<url> on the loopback API.
    // Beta1.0.7: the URL defaults to GitHub and the threshold is 5000 ms —
    // a node answering inside 5 s reports its real milliseconds; only a node
    // with no answer at all is a timeout.
    long long TestDelay(const std::wstring& node,
                        int timeout_ms = 5000,
                        const std::wstring& url = L"https://github.com");

    // Every node in parallel; results sorted by latency (timeouts last).
    std::vector<DelayResult> TestAllDelays(
        const std::vector<std::wstring>& nodes,
        int timeout_ms = 5000,
        const std::wstring& url = L"https://github.com");

    // PUT /proxies/<group> {"name": node}
    bool SelectNode(const std::wstring& group, const std::wstring& node);

    int api_port() const { return api_port_; }

    // GET a path on the loopback API (used to read back /proxies JSON).
    std::wstring HttpGet(const std::wstring& path, int timeout_ms, bool* ok);

private:
    bool HttpPut(const std::wstring& path, const std::wstring& body,
                 int timeout_ms);

    HANDLE process_ = nullptr;
    int api_port_ = 0;
    int mixed_port_ = 0;
};

}  // namespace linkhelm
