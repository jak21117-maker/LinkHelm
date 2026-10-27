// LinkHelm relay — see relay.h. Mirrors laptop_proxy/relay.py (beta1.0.8).
#include "relay.h"

#include <chrono>

namespace linkhelm {

const char* kLaneStore = "store";
const char* kLaneTransfer = "transfer";

namespace {

long long NowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

constexpr int kMaxPressure = 3;
constexpr long long kPressureTtlMs = 8000;
constexpr int kBackoffMs[kMaxPressure + 1] = {0, 5, 20, 50};

}  // namespace

Relay& Relay::instance() {
    static Relay r;
    return r;
}

void Relay::Claim(const std::wstring& lane, const std::wstring& owner) {
    std::lock_guard<std::mutex> lk(mu_);
    owners_[lane] = owner;  // dormant until the first claim arrives
}

void Relay::Release(const std::wstring& lane, const std::wstring& owner) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = owners_.find(lane);
    if (it != owners_.end() && it->second == owner) owners_.erase(it);
    // Both optional features stopped -> back to silent standby (no timers,
    // no threads; the map being empty IS the standby state).
}

bool Relay::IsActive(const std::wstring& lane) const {
    std::lock_guard<std::mutex> lk(mu_);
    return owners_.find(lane) != owners_.end();
}

bool Relay::IsIdle() const {
    std::lock_guard<std::mutex> lk(mu_);
    return owners_.empty();
}

void Relay::SetNetworkPressure(int level) {
    std::lock_guard<std::mutex> lk(mu_);
    if (level < 0) level = 0;
    if (level > kMaxPressure) level = kMaxPressure;
    pressure_ = level;
    pressure_at_ms_ = NowMs();
}

int Relay::PressureLevel() const {
    std::lock_guard<std::mutex> lk(mu_);
    if (pressure_ && NowMs() - pressure_at_ms_ > kPressureTtlMs) {
        pressure_ = 0;  // decayed to calm
    }
    return pressure_;
}

int Relay::TransferBackoffMs() const {
    return kBackoffMs[PressureLevel()];
}

bool Relay::MayTouchSystemState(const std::wstring& lane,
                                const std::wstring& what) {
    if (lane == L"store" &&
        (what == L"system_proxy" || what == L"routes" || what == L"dns")) {
        return false;  // store lane is loopback-only (relay policy)
    }
    return true;
}

}  // namespace linkhelm
