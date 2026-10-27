// LinkHelm tunnel manager (beta1.0.85) — C++ port of the Python TunManager
// core flow: SOCKS5 precheck -> engine config -> engine spawn (via the
// supervisor's job object) -> adapter config (netops) -> /1 routes ->
// health monitor -> clean teardown. UI layout/behaviour matches the py app.

#pragma once
#include <windows.h>
#include <atomic>
#include <functional>
#include <string>
#include <thread>
#include <vector>

namespace linkhelm {

struct TunnelConfig {
    std::wstring phone_ip;      // e.g. 192.168.49.1
    int phone_port = 8080;      // SOCKS5 on the phone
    int local_port = 18081;     // local HTTP->SOCKS bridge port (legacy compat)
    std::wstring socks_user;    // 8-13 alnum
    std::wstring socks_pass;
};

class TunnelManager {
public:
    using LogFn = std::function<void(const std::wstring&)>;
    using StateFn = std::function<void(const std::wstring&)>;  // status line

    explicit TunnelManager(LogFn log, StateFn status);
    ~TunnelManager();

    // Full connect sequence; returns false with *error filled on failure.
    // Runs BLOCKING — callers put it on a worker thread.
    bool Start(const TunnelConfig& cfg, std::wstring* error);
    void Stop();
    bool IsUp() const { return up_.load(); }

private:
    bool PrecheckSocks5(const TunnelConfig& cfg, std::wstring* error);
    bool WriteEngineConfig(const TunnelConfig& cfg, std::wstring* path);
    bool LaunchEngine(const std::wstring& cfg_path, std::wstring* error);
    bool WaitAdapter(int seconds, std::wstring* conn_name);
    bool ConfigureAdapter(const std::wstring& conn_name);
    bool AddRoutes(const std::wstring& conn_name, std::wstring* error);
    void CleanRoutes();
    void MonitorLoop();

    LogFn log_;
    StateFn status_;
    std::atomic<bool> up_{false};
    std::atomic<bool> monitoring_{false};
    std::thread monitor_;
    HANDLE engine_ = nullptr;      // process handle (job-owned)
    DWORD engine_pid_ = 0;
    std::wstring conn_name_;       // resolved adapter connection name
    std::wstring phone_ip_;
    std::wstring phone_port_;
    std::wstring phone_route_gw_;

    struct Impl;
    Impl* impl_ = nullptr;
};

}  // namespace linkhelm
