// FT harness 2: real receiver module; accepts ONE file into <dir>, prints
// RECV-OK when verified. Usage: ft_recv_test.exe <save_dir>
#include "ft_receiver.h"
#include <cstdio>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    if (argc < 2) { printf("usage: ft_recv_test <dir>\n"); return 2; }
    std::wstring dir;
    for (char* p = argv[1]; *p; ++p) dir += (wchar_t)*p;
    linkhelm::ReceiverServer rx("", "linkhelm01", "k7h2m9x4qa", dir,
        [](const std::wstring& m) { wprintf(L"[log] %s\n", m.c_str()); },
        [](const std::wstring& m) { wprintf(L"[st] %s\n", m.c_str()); });
    rx.Start();
    rx.ArmOneFile();
    // run 30s max
    for (int i = 0; i < 300; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return 0;
}
