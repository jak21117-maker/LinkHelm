// Standalone verification harness for the tray-menu language switch.
// Compiles the REAL tray.cpp with stubbed Win32 GDI/User calls so we can
// assert that HandleIconMessage builds an English menu in English mode and a
// Chinese menu in Chinese mode, without launching a real window.
#include "tray.h"

#include <cstdio>
#include <cstring>
#include <vector>
#include <string>

// ---- captured menu strings ----
static std::vector<std::wstring> g_menu;
static bool g_gotMenu = false;

// ---- minimal Win32 stubs (signatures mirror windows.h) ----
extern "C" {
HDC GetDC(HWND) { return (HDC)1; }
int ReleaseDC(HWND, HDC) { return 1; }
HDC CreateCompatibleDC(HDC) { return (HDC)2; }
HBITMAP CreateCompatibleBitmap(HDC, int, int) { return (HBITMAP)3; }
HGDIOBJ SelectObject(HDC, HGDIOBJ) { return (HGDIOBJ)4; }
HBRUSH CreateSolidBrush(COLORREF) { return (HBRUSH)5; }
int FillRect(HDC, const RECT*, HBRUSH) { return 1; }
int Ellipse(HDC, int, int, int, int) { return 1; }
int DeleteObject(HGDIOBJ) { return 1; }
int DeleteDC(HDC) { return 1; }
HICON CreateIconIndirect(PICONINFO) { return (HICON)6; }

HMENU CreatePopupMenu() { g_menu.clear(); g_gotMenu = true; return (HMENU)7; }
BOOL AppendMenuW(HMENU, UINT, UINT_PTR id, LPCWSTR text) {
    (void)id; (void)text;
    if (text) g_menu.emplace_back(text);
    return TRUE;
}
BOOL DestroyMenu(HMENU) { return TRUE; }
BOOL SetForegroundWindow(HWND) { return TRUE; }
BOOL TrackPopupMenu(HMENU, UINT, int, int, int, HWND, const RECT*) { return TRUE; }
BOOL GetCursorPos(LPPOINT p) { if (p) { p->x = 0; p->y = 0; } return TRUE; }
BOOL Shell_NotifyIconW(DWORD, PNOTIFYICONDATAW) { return TRUE; }
}

static int failures = 0;
static void check(bool cond, const char* msg) {
    printf("%s: %s\n", cond ? "PASS" : "FAIL", msg);
    if (!cond) failures++;
}

int main() {
    // ---- English mode ----
    linkhelm::Tray tray(nullptr, nullptr, L"LinkHelm");
    tray.SetLanguage(false);
    g_menu.clear(); g_gotMenu = false;
    tray.HandleIconMessage(linkhelm::Tray::kTrayMessage, 0, (LPARAM)WM_RBUTTONUP);
    check(g_gotMenu, "English: popup menu built on RBUTTONUP");
    check(g_menu.size() == 4, "English: 4 menu entries (Show/Hide/Settings/Quit)");
    if (g_menu.size() == 4) {
        check(g_menu[0] == L"Show Window", "English: item0 = Show Window");
        check(g_menu[1] == L"Hide to Tray", "English: item1 = Hide to Tray");
        check(g_menu[2] == L"Settings", "English: item2 = Settings");
        check(g_menu[3] == L"Quit & Clean Up Network",
              "English: item3 = Quit & Clean Up Network");
    }

    // ---- Chinese mode ----
    tray.SetLanguage(true);
    g_menu.clear(); g_gotMenu = false;
    tray.HandleIconMessage(linkhelm::Tray::kTrayMessage, 0, (LPARAM)WM_RBUTTONUP);
    check(g_gotMenu, "Chinese: popup menu built on RBUTTONUP");
    check(g_menu.size() == 4, "Chinese: 4 menu entries");
    if (g_menu.size() == 4) {
        // 显示窗口 / 隐藏到托盘 / 设置 / 退出并清理网络
        check(g_menu[0] == L"\u663e\u793a\u7a97\u53e3", "Chinese: item0 = \u663e\u793a\u7a97\u53e3");
        check(g_menu[1] == L"\u9690\u85cf\u5230\u6258\u76d8", "Chinese: item1 = \u9690\u85cf\u5230\u6258\u76d8");
        check(g_menu[2] == L"\u8bbe\u7f6e", "Chinese: item2 = \u8bbe\u7f6e");
        check(g_menu[3] == L"\u9000\u51fa\u5e76\u6e05\u7406\u7f51\u7edc",
              "Chinese: item3 = \u9000\u51fa\u5e76\u6e05\u7406\u7f51\u7edc");
    }

    printf("\n%s\n", failures == 0 ? "ALL TRAy LANGUAGE TESTS PASSED"
                                   : "SOME TRAy LANGUAGE TESTS FAILED");
    return failures;
}
