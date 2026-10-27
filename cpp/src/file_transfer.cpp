// LinkHelm file transfer — implementation. See file_transfer.h.

#include "file_transfer.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <wincrypt.h>
#include <shellapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <thread>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "advapi32.lib")

namespace linkhelm {
namespace {

constexpr unsigned long long kSpeedCap = 100ULL * 1024 * 1024;  // 100MB/s
constexpr size_t kChunkSmall = 128 * 1024;
constexpr size_t kChunkLarge = 4 * 1024 * 1024;

std::string Utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), &out[0], n,
                        nullptr, nullptr);
    return out;
}
std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                                nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n);
    return out;
}

// base64 (standard alphabet, no line wraps)
std::string B64(const std::string& in) {
    static const char* T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    size_t i = 0;
    while (i + 2 < in.size()) {
        unsigned v = (unsigned char)in[i] << 16 | (unsigned char)in[i + 1] << 8 |
                     (unsigned char)in[i + 2];
        out += T[(v >> 18) & 63]; out += T[(v >> 12) & 63];
        out += T[(v >> 6) & 63];  out += T[v & 63];
        i += 3;
    }
    if (i + 1 == in.size()) {
        unsigned v = (unsigned char)in[i] << 16;
        out += T[(v >> 18) & 63]; out += T[(v >> 12) & 63]; out += "==";
    } else if (i + 2 == in.size()) {
        unsigned v = (unsigned char)in[i] << 16 | (unsigned char)in[i + 1] << 8;
        out += T[(v >> 18) & 63]; out += T[(v >> 12) & 63];
        out += T[(v >> 6) & 63];  out += '=';
    }
    return out;
}

bool SendLine(SOCKET s, const std::string& line) {
    std::string d = line + "\n";
    const char* p = d.data();
    int left = (int)d.size();
    while (left > 0) {
        int n = send(s, p, left, 0);
        if (n <= 0) return false;
        p += n; left -= n;
    }
    return true;
}

std::string RecvLine(SOCKET s, int timeout_sec = 60) {
    std::string out;
    char c = 0;
    u_long ms = timeout_sec * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char*)&ms, sizeof(ms));
    while (true) {
        int n = recv(s, &c, 1, 0);
        if (n <= 0) return out;
        if (c == '\n') break;
        if (c != '\r') out += c;
        if (out.size() > 4096) break;
    }
    return out;
}

bool SendAll(SOCKET s, const char* p, size_t len) {
    while (len > 0) {
        int n = send(s, p, (int)len, 0);
        if (n <= 0) return false;
        p += n; len -= (size_t)n;
    }
    return true;
}

}  // namespace

std::string FileTransfer::Sha256File(const std::wstring& path) {
    HCRYPTPROV prov = 0;
    HCRYPTHASH hash = 0;
    if (!CryptAcquireContextW(&prov, nullptr, nullptr, PROV_RSA_AES,
                              CRYPT_VERIFYCONTEXT) ||
        !CryptCreateHash(prov, CALG_SHA_256, 0, 0, &hash)) {
        if (prov) CryptReleaseContext(prov, 0);
        return "";
    }
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                           nullptr, OPEN_EXISTING,
                           FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        std::string buf(kChunkLarge, '\0');
        for (;;) {
            DWORD got = 0;
            if (!ReadFile(h, &buf[0], (DWORD)kChunkLarge, &got, nullptr) ||
                got == 0)
                break;
            if (!CryptHashData(hash, (const BYTE*)buf.data(), got, 0)) break;
            if (got < (DWORD)kChunkLarge) break;
        }
        CloseHandle(h);
    }
    DWORD size = 0;
    DWORD len = sizeof(size);
    CryptGetHashParam(hash, HP_HASHSIZE, (BYTE*)&size, &len, 0);
    std::string digest(size, '\0');
    len = size;
    CryptGetHashParam(hash, HP_HASHVAL, (BYTE*)&digest[0], &len, 0);
    CryptDestroyHash(hash);
    CryptReleaseContext(prov, 0);
    static const char* H = "0123456789abcdef";
    std::string hex;
    for (BYTE b : digest) { hex += H[b >> 4]; hex += H[b & 15]; }
    return hex;
}

bool FileTransfer::SendFile(const std::string& target_ip, int port,
                            const std::string& user, const std::string& pass,
                            const std::wstring& file_path, ProgressFn progress) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    // size
    HANDLE hf = CreateFileW(file_path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                            nullptr, OPEN_EXISTING, 0, nullptr);
    if (hf == INVALID_HANDLE_VALUE) {
        if (log_) log_(L"cannot open file");
        return false;
    }
    LARGE_INTEGER sz{};
    GetFileSizeEx(hf, &sz);
    unsigned long long size = (unsigned long long)sz.QuadPart;
    CloseHandle(hf);
    if (size == 0) {
        if (log_) log_(L"file is empty");
        return false;
    }
    // name
    std::wstring name = file_path.substr(file_path.find_last_of(L"\\/") + 1);
    std::string name8 = Utf8(name);
    std::string digest = Sha256File(file_path);

    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    inet_pton(AF_INET, target_ip.c_str(), &addr.sin_addr);
    if (connect(s, (sockaddr*)&addr, sizeof(addr)) != 0) {
        if (log_) log_(L"connect to receiver failed (" + Utf16(target_ip) + L")");
        closesocket(s);
        return false;
    }
    bool ok = false;
    do {
        if (!SendLine(s, "AUTH " + user + " " + pass)) break;
        std::string r = RecvLine(s);
        if (r != "OK") {
            if (log_) log_(L"auth rejected: " + Utf16(r));
            break;
        }
        if (!SendLine(s, "OFFER " + B64(name8) + " " +
                         std::to_string(size) + " " + digest))
            break;
        r = RecvLine(s);
        unsigned long long offset = 0;
        if (r.rfind("RESUME ", 0) == 0) {
            offset = _strtoui64(r.c_str() + 7, nullptr, 10);
            if (log_) log_(L"resuming at " + std::to_wstring(offset));
        } else if (r != "ACCEPT") {
            if (log_) log_(L"receiver refused: " + Utf16(r));
            break;
        }
        // TCP_NODELAY
        int one = 1;
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char*)&one, sizeof(one));
        size_t chunk = size >= 64ULL * 1024 * 1024 ? kChunkLarge : kChunkSmall;
        // byte-budget pacing (cap 100MB/s, half-chunk slack — measured)
        auto origin = std::chrono::steady_clock::now();
        unsigned long long slack = chunk / 2;
        HANDLE h = CreateFileW(file_path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                               nullptr, OPEN_EXISTING,
                               FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
        if (h == INVALID_HANDLE_VALUE) break;
        if (offset) SetFilePointer(h, (LONG)offset, nullptr, FILE_BEGIN);
        std::string buf(chunk, '\0');
        unsigned long long sent = offset;
        bool io = true;
        while (sent < size) {
            // pacing: block while sent-offset > elapsed*cap + slack
            double el = std::chrono::duration<double>(
                            std::chrono::steady_clock::now() - origin).count();
            unsigned long long budget = (unsigned long long)(el * (double)kSpeedCap) + slack;
            if (sent - offset > budget) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                continue;
            }
            DWORD want = (DWORD)(std::min)((unsigned long long)chunk, size - sent);
            DWORD got = 0;
            if (!ReadFile(h, &buf[0], want, &got, nullptr) || got == 0) {
                io = false; break;
            }
            if (!SendAll(s, buf.data(), got)) { io = false; break; }
            sent += got;
            if (progress) progress(sent, size);
        }
        CloseHandle(h);
        if (!io) {
            if (log_) log_(L"transfer aborted (IO/connection error)");
            break;
        }
        r = RecvLine(s, 120);
        if (r != "DONE") {
            if (log_) log_(L"receiver verify failed: " + Utf16(r));
            break;
        }
        ok = true;
        if (log_) log_(name + L" delivered and verified (SHA-256)");
    } while (false);
    closesocket(s);
    return ok;
}

}  // namespace linkhelm
