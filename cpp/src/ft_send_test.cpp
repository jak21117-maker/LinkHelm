// FT harness 1: real sender module against a live receiver.
// Usage: ft_send_test.exe <ip> <file>
#include "file_transfer.h"
#include <cstdio>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) { printf("usage: ft_send_test <ip> <file>\n"); return 2; }
    linkhelm::FileTransfer ft(
        [](const std::wstring& m) { wprintf(L"[log] %s\n", m.c_str()); });
    // wide path from argv (assume ASCII test paths)
    std::wstring file;
    for (char* p = argv[2]; *p; ++p) file += (wchar_t)*p;
    bool ok = ft.SendFile(argv[1], 8765, "linkhelm01", "k7h2m9x4qa",
                          file, [](unsigned long long s, unsigned long long t) {
                              if (s == t)
                                  wprintf(L"[progress] done %llu/%llu\n", s, t);
                          });
    printf(ok ? "SENT-OK\n" : "SENT-FAIL\n");
    return ok ? 0 : 1;
}
