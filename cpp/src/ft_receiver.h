// LinkHelm file transfer receiver + pairing (beta1.0.89).
// Wire protocol matches the Android/py side:
//   PAIR <user> <pass>          -> PAIRED | ERR busy
//   AUTH/OFFER/data             -> the file receive path
// One listener thread serves pairing any time; Receive mode saves into the
// chosen folder; send-side keeps the 1.0.85 sender unchanged.

#pragma once
#include <windows.h>
#include <atomic>
#include <functional>
#include <string>

namespace linkhelm {

class ReceiverServer {
public:
    using LogFn = std::function<void(const std::wstring&)>;
    using StatusFn = std::function<void(const std::wstring&)>;

    ReceiverServer(std::string bind_ip, std::string user, std::string pass,
                   std::wstring save_dir, LogFn log, StatusFn status)
        : bind_ip_(std::move(bind_ip)),
          user_(std::move(user)),
          pass_(std::move(pass)),
          save_dir_(std::move(save_dir)),
          log_(std::move(log)),
          status_(std::move(status)) {}
    ~ReceiverServer() { Stop(); }

    void Start();
    void Stop();
    bool Running() const { return running_.load(); }
    void SetSaveDir(const std::wstring& d) { save_dir_ = d; }
    // one-shot: accept ONE file then keep pairing-only
    void ArmOneFile();

private:
    void Loop();
    void ServeClient(void* client);

    std::string bind_ip_, user_, pass_;
    std::wstring save_dir_;
    LogFn log_;
    StatusFn status_;
    std::atomic<bool> running_{false};
    std::atomic<bool> armed_{false};   // true -> accept AUTH/OFFER
    void* sock_ = nullptr;             // listen socket
    void* thread_ = nullptr;
};

}  // namespace linkhelm
