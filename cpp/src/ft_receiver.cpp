// LinkHelm file transfer receiver — implementation. See ft_receiver.h.
#include "ft_receiver.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <wincrypt.h>
#include <shlwapi.h>

#include <cstdio>
#include <fstream>
#include <thread>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "advapi32.lib")

namespace linkhelm {
namespace {

constexpr int kFtPort = 8765;

std::wstring Utf16(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                                nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n);
    return out;
}
std::string Utf8w(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), &out[0], n,
                        nullptr, nullptr);
    return out;
}

// minimal base64 decode for OFFER names
int B64Val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}
std::string B64Dec(const std::string& in) {
    std::string out;
    int val = 0, valb = -8;
    for (char c : in) {
        int v = B64Val(c);
        if (v < 0) continue;
        val = (val << 6) | v;
        valb += 6;
        if (valb >= 0) {
            out.push_back(char((val >> valb) & 0xFF));
            valb -= 8;
        }
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
        if (out.size() > 8192) break;
    }
    return out;
}

std::string Sha256Hex(const std::wstring& path) {
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
        std::string buf(4 << 20, '\0');
        for (;;) {
            DWORD got = 0;
            if (!ReadFile(h, &buf[0], (DWORD)buf.size(), &got, nullptr) ||
                got == 0)
                break;
            if (!CryptHashData(hash, (const BYTE*)buf.data(), got, 0)) break;
            if (got < buf.size()) break;
        }
        CloseHandle(h);
    }
    DWORD len = 32;
    std::string digest(32, '\0');
    CryptGetHashParam(hash, HP_HASHVAL, (BYTE*)&digest[0], &len, 0);
    CryptDestroyHash(hash);
    CryptReleaseContext(prov, 0);
    static const char* H = "0123456789abcdef";
    std::string hex;
    for (BYTE b : digest) { hex += H[b >> 4]; hex += H[b & 15]; }
    return hex;
}

}  // namespace

void ReceiverServer::Start() {
    if (running_.exchange(true)) return;
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    auto* t = new std::thread([this] { Loop(); });
    thread_ = t;
}

void ReceiverServer::Stop() {
    running_.store(false);
    if (sock_) {
        closesocket((SOCKET)(intptr_t)sock_);
        sock_ = nullptr;
    }
    if (thread_) {
        auto* t = (std::thread*)thread_;
        if (t->joinable()) t->join();
        delete t;
        thread_ = nullptr;
    }
}

void ReceiverServer::ArmOneFile() { armed_.store(true); }

void ReceiverServer::Loop() {
    SOCKET ls = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, (const char*)&one, sizeof(one));
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(kFtPort);
    if (bind_ip_.empty() || bind_ip_ == "0.0.0.0") {
        a.sin_addr.s_addr = INADDR_ANY;
    } else {
        inet_pton(AF_INET, bind_ip_.c_str(), &a.sin_addr);
    }
    if (bind(ls, (sockaddr*)&a, sizeof(a)) != 0 || listen(ls, 3) != 0) {
        if (log_) log_(L"[ft] receiver bind failed (port in use?)");
        closesocket(ls);
        running_.store(false);
        return;
    }
    sock_ = (void*)(intptr_t)ls;
    if (log_) log_(L"[ft] receiver listening (pairing always on)");
    while (running_.load()) {
        sockaddr_in ca{};
        int cl = sizeof(ca);
        SOCKET c = accept(ls, (sockaddr*)&ca, &cl);
        if (c == INVALID_SOCKET) break;
        // serve inline (single-file semantics; pairing never blocks long)
        ServeClient((void*)(intptr_t)c);
    }
}

void ReceiverServer::ServeClient(void* client) {
    SOCKET s = (SOCKET)(intptr_t)client;
    bool served = false;
    // first line decides: PAIR / AUTH
    for (int round = 0; round < 4 && running_.load(); ++round) {
        std::string line = RecvLine(s, 20);
        if (line.empty()) break;
        if (line.rfind("PAIR ", 0) == 0) {
            // pairing handshake (credentials checked by sender protocol)
            SendLine(s, "PAIRED");
            if (log_) log_(L"[ft] peer paired");
            continue;
        }
        if (line.rfind("AUTH ", 0) == 0) {
            SendLine(s, "OK");
            std::string offer = RecvLine(s, 30);
            if (offer.rfind("OFFER ", 0) != 0) break;
            // OFFER <b64name> <size> <sha>
            size_t p1 = offer.find(' ', 6);
            size_t p2 = offer.find(' ', p1 + 1);
            std::string b64 = offer.substr(6, p1 - 6);
            unsigned long long size =
                _strtoui64(offer.substr(p1 + 1, p2 - p1 - 1).c_str(),
                           nullptr, 10);
            std::string sha = offer.substr(p2 + 1);
            std::string name8 = B64Dec(b64);
            std::wstring name = Utf16(name8);
            // sanitize name
            for (wchar_t& ch : name) {
                if (wcschr(L"\\/:*?\"<>|", ch)) ch = L'_';
            }
            if (!armed_.load()) {
                SendLine(s, "ERR busy");
                if (log_) log_(L"[ft] file offered while not armed -> busy");
                break;
            }
            // resume support: existing .part with matching meta
            std::wstring dir = save_dir_.empty() ? L"." : save_dir_;
            std::wstring part = dir + L"\\" + name + L".part";
            unsigned long long offset = 0;
            HANDLE hf = CreateFileW(part.c_str(), GENERIC_READ, FILE_SHARE_READ,
                                    nullptr, OPEN_EXISTING, 0, nullptr);
            if (hf != INVALID_HANDLE_VALUE) {
                LARGE_INTEGER sz{};
                GetFileSizeEx(hf, &sz);
                CloseHandle(hf);
                if (sz.QuadPart > 0 && sz.QuadPart < (long long)size) {
                    offset = (unsigned long long)sz.QuadPart;
                }
            }
            if (offset) {
                std::string r = "RESUME " + std::to_string(offset);
                SendLine(s, r);
                if (log_)
                    log_(L"[ft] resuming at " + std::to_wstring(offset));
            } else {
                SendLine(s, "ACCEPT");
            }
            // receive
            HANDLE ho = CreateFileW(part.c_str(), GENERIC_WRITE, 0, nullptr,
                                    OPEN_ALWAYS, 0, nullptr);
            if (ho == INVALID_HANDLE_VALUE) {
                SendLine(s, "ERR io");
                break;
            }
            if (offset)
                SetFilePointer(ho, 0, nullptr, FILE_END);
            unsigned long long got = offset;
            std::string buf(256 << 10, '\0');
            bool iok = true;
            while (got < size) {
                DWORD want =
                    (DWORD)(std::min<unsigned long long>)(buf.size(),
                                                          size - got);
                DWORD recvd = 0;
                int n = recv(s, &buf[0], (int)want, 0);
                if (n <= 0) { iok = false; break; }
                if (!WriteFile(ho, buf.data(), (DWORD)n, &recvd, nullptr)) {
                    iok = false;
                    break;
                }
                got += (DWORD)n;
                if (status_)
                    status_(L"Receiving: " + std::to_wstring(got / 1048576) +
                            L" / " + std::to_wstring(size / 1048576) + L" MB");
            }
            CloseHandle(ho);
            if (!iok) {
                if (log_) log_(L"[ft] receive interrupted; partial kept");
                break;
            }
            // verify
            std::wstring finalPath = dir + L"\\" + name;
            std::string gotSha = Sha256Hex(part);
            if (gotSha == sha) {
                MoveFileExW(part.c_str(), finalPath.c_str(),
                            MOVEFILE_REPLACE_EXISTING);
                SendLine(s, "DONE");
                if (log_) log_(name + L" received & verified");
            } else {
                SendLine(s, "ERR hash");
                if (log_) log_(L"[ft] hash mismatch; .part kept for resume");
            }
            // beta1.0.92: stay armed (py 1.0.61) — the listener keeps
            // accepting the next file until the user cancels.
            served = true;
            break;
        }
        break;
    }
    closesocket(s);
    if (served && status_)
        status_(L"Receive complete");
}

}  // namespace linkhelm
