// LinkHelm — native tray (Shell_NotifyIcon), no third-party dependency.
// beta1.0.7 / C++ rewrite P1. Replaces pystray, which silently disappears
// whenever the packaging venv lacks it (that is exactly how 1.0.64 lost
// tray residency and closed the window straight into an exit).
#pragma once

#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <shellapi.h>
#include <string>

namespace linkhelm {

// draws the brand icon (blue rounded tile + white helmet) in code; used for
// BOTH the tray icon and the window/taskbar icon.
HICON MakeBrandIcon();      // 64px tray
HICON MakeBrandIconSmall();  // 16px taskbar/titlebar

enum class TrayAction {
    None = 0,
    ShowWindow,
    HideToTray,
    Settings,
    QuitAndCleanup,
};

class Tray {
public:
    // hwnd receives WM_TRAY_MESSAGE (see kTrayMessage) on tray interaction;
    // the owner then calls Pump() to pull queued actions onto its own thread.
    explicit Tray(HWND hwnd, HINSTANCE inst, const std::wstring& tip);
    ~Tray();

    bool Install();   // adds the icon; false = no tray available
    void Remove();
    void ShowBalloon(const std::wstring& title, const std::wstring& text);

    // Thread-safe action queue: the tray callback only ENQUEUES, the GUI
    // thread consumes — direct cross-thread window calls are what make a
    // Python tray wedge the whole app.
    void Push(TrayAction action);
    TrayAction Pump();

    // The icon posts to the window that owns it, so the owner's WndProc must
    // forward kTrayMessage / WM_COMMAND here. Returns true when handled.
    bool HandleIconMessage(UINT msg, WPARAM wp, LPARAM lp);

    // beta1.0.92: tray menu follows the app language (true = Chinese).
    void SetLanguage(bool zh) { lang_zh_ = zh; }

    static const UINT kTrayMessage = WM_APP + 0x2f;
    static const UINT kTrayUid = 1;

private:
    static LRESULT CALLBACK IconProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

    HWND hwnd_ = nullptr;
    HINSTANCE inst_ = nullptr;
    NOTIFYICONDATAW nid_{};
    bool installed_ = false;
    bool lang_zh_ = false;
    CRITICAL_SECTION lock_{};
    TrayAction pending_ = TrayAction::None;
};

}  // namespace linkhelm
