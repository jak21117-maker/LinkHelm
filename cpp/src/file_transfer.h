// LinkHelm file transfer (beta1.0.85) — pure C++ sender/receiver.
// Wire-compatible with the Android/Python receiver protocol:
//   AUTH <user> <pass>            -> OK | ERR
//   OFFER <name-b64> <size> <sha> -> ACCEPT | RESUME <off> | ERR
//   <raw bytes>                   -> DONE | ERR <msg>
// Speed (user spec): adaptive chunks, tens of MB/s, hard cap 100 MB/s
// via byte-budget pacing (measured on loopback against the Python peer).

#pragma once
#include <windows.h>
#include <functional>
#include <string>

namespace linkhelm {

struct SendProgress {
    unsigned long long sent = 0;
    unsigned long long total = 0;
};

class FileTransfer {
public:
    using LogFn = std::function<void(const std::wstring&)>;
    using ProgressFn = std::function<void(unsigned long long, unsigned long long)>;

    explicit FileTransfer(LogFn log) : log_(std::move(log)) {}

    // Send one file (blocks; caller runs on a worker thread).
    bool SendFile(const std::string& target_ip, int port,
                  const std::string& user, const std::string& pass,
                  const std::wstring& file_path,
                  ProgressFn progress = nullptr);

    // SHA-256 hex of a file (streamed, 4MB blocks).
    static std::string Sha256File(const std::wstring& path);

private:
    LogFn log_;
};

}  // namespace linkhelm
