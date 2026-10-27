// LinkHelm relay — beta1.0.8 business-lane coordinator (C++ mirror).
// Same contract as laptop_proxy/relay.py: DORMANT by default; wakes only
// when the store booster actually started or a file transfer is in flight;
// returns to silent standby when both stop. Control-plane only, no data.
#pragma once

#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace linkhelm {

extern const char* kLaneStore;
extern const char* kLaneTransfer;

class Relay {
public:
    static Relay& instance();

    // Lane ownership. Re-claiming the same lane with the same owner is a
    // no-op; releasing by a different owner is ignored.
    void Claim(const std::wstring& lane, const std::wstring& owner);
    void Release(const std::wstring& lane, const std::wstring& owner);
    bool IsActive(const std::wstring& lane) const;
    bool IsIdle() const;   // true = silent standby (no optional feature)

    // Network-pressure scheduling: 0 calm .. 3 heavy. Values decay to calm
    // after ~8s without a fresh report. While heavy, an ACTIVE transfer
    // backs off per chunk (TransferBackoffMs).
    void SetNetworkPressure(int level);
    int PressureLevel() const;
    int TransferBackoffMs() const;

    // Policy: the store lane is loopback-only and must never write the
    // system proxy, routes or DNS (that is what made the features 打架).
    static bool MayTouchSystemState(const std::wstring& lane,
                                    const std::wstring& what);

private:
    mutable std::mutex mu_;
    std::map<std::wstring, std::wstring> owners_;
    mutable int pressure_ = 0;
    mutable long long pressure_at_ms_ = 0;
};

}  // namespace linkhelm
