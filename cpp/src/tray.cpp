// LinkHelm — native tray implementation (see tray.h).
#include "tray.h"

#include <shellapi.h>
#include <windows.h>

namespace linkhelm {

static HICON DrawBrandIcon(int S);

// beta1.0.85: brand icon drawn in code — same look as the py app's PIL icon
// (rounded blue tile + white helmet circle + visor bar). No external file.
static HICON DrawBrandIcon(int S) {
    HDC dc = GetDC(nullptr);
    HDC mem = CreateCompatibleDC(dc);
    HBITMAP bm = CreateCompatibleBitmap(dc, S, S);
    HGDIOBJ old = SelectObject(mem, bm);
    // transparent background
    {
        HBRUSH bg = CreateSolidBrush(RGB(0, 0, 0));
        RECT rAll{0, 0, S, S};
        // draw nothing; alpha handled via mask below
        (void)bg; (void)rAll;
    }
    // blue rounded tile (RGB 37,99,235 like the py version), scaled
    double k = S / 64.0;
    HBRUSH blue = CreateSolidBrush(RGB(37, 99, 235));
    RECT rTile{(int)(4 * k), (int)(4 * k), (int)(60 * k), (int)(60 * k)};
    FillRect(mem, &rTile, blue);
    // white helmet circle + visor bar
    HBRUSH white = CreateSolidBrush(RGB(255, 255, 255));
    HBRUSH oldB = (HBRUSH)SelectObject(mem, white);
    Ellipse(mem, (int)(24 * k), (int)(16 * k), (int)(40 * k), (int)(32 * k));
    RECT rBar{(int)(18 * k), (int)(40 * k), (int)(46 * k), (int)(45 * k)};
    FillRect(mem, &rBar, white);
    SelectObject(mem, oldB);
    DeleteObject(blue);
    SelectObject(mem, oldB);
    DeleteObject(white);
    // icon from bitmap + opaque mask (square tile look is fine at tray size)
    HICON icon = nullptr;
    ICONINFO ii{};
    ii.fIcon = TRUE;
    ii.hbmColor = bm;
    ii.hbmMask = bm;  // same bitmap as mask (no transparency needed)
    icon = CreateIconIndirect(&ii);
    SelectObject(mem, old);
    DeleteObject(bm);
    DeleteDC(mem);
    ReleaseDC(nullptr, dc);
    return icon;
}

static const UINT kMenuShow = 1001;
static const UINT kMenuHide = 1002;
static const UINT kMenuSettings = 1003;
static const UINT kMenuQuit = 1004;

Tray::Tray(HWND hwnd, HINSTANCE inst, const std::wstring& tip)
    : hwnd_(hwnd), inst_(inst) {
    InitializeCriticalSection(&lock_);
    ZeroMemory(&nid_, sizeof(nid_));
    nid_.cbSize = sizeof(nid_);
    nid_.hWnd = hwnd_;
    nid_.uID = kTrayUid;
    nid_.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    nid_.uCallbackMessage = kTrayMessage;
    // beta1.0.85 brand icon (drawn, matches the py app's tray look)
    HICON brand = MakeBrandIcon();
    nid_.hIcon = brand ? brand : LoadIconW(nullptr, IDI_APPLICATION);
    wcsncpy_s(nid_.szTip, tip.c_str(), _TRUNCATE);
}

Tray::~Tray() {
    Remove();
    DeleteCriticalSection(&lock_);
}

bool Tray::Install() {
    if (installed_) return true;
    installed_ = Shell_NotifyIconW(NIM_ADD, &nid_) != FALSE;
    if (installed_) {
        nid_.uVersion = NOTIFYICON_VERSION_4;
        Shell_NotifyIconW(NIM_SETVERSION, &nid_);
    }
    return installed_;
}

void Tray::Remove() {
    if (!installed_) return;
    Shell_NotifyIconW(NIM_DELETE, &nid_);
    installed_ = false;
}

void Tray::ShowBalloon(const std::wstring& title, const std::wstring& text) {
    if (!installed_) return;
    NOTIFYICONDATAW n = nid_;
    n.uFlags |= NIF_INFO;
    n.dwInfoFlags = NIIF_INFO;
    wcsncpy_s(n.szInfoTitle, title.c_str(), _TRUNCATE);
    wcsncpy_s(n.szInfo, text.c_str(), _TRUNCATE);
    Shell_NotifyIconW(NIM_MODIFY, &n);
}

void Tray::Push(TrayAction action) {
    EnterCriticalSection(&lock_);
    pending_ = action;
    LeaveCriticalSection(&lock_);
}

TrayAction Tray::Pump() {
    EnterCriticalSection(&lock_);
    TrayAction a = pending_;
    pending_ = TrayAction::None;
    LeaveCriticalSection(&lock_);
    return a;
}

bool Tray::HandleIconMessage(UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == kTrayMessage) {
        switch (LOWORD(lp)) {
            case WM_LBUTTONDBLCLK:
                Push(TrayAction::ShowWindow);
                return true;
            case WM_RBUTTONUP: {
                POINT pt{};
                GetCursorPos(&pt);
                HMENU menu = CreatePopupMenu();
                if (lang_zh_) {
                    AppendMenuW(menu, MF_STRING, kMenuShow, L"\u663e\u793a\u7a97\u53e3");
                    AppendMenuW(menu, MF_STRING, kMenuHide, L"\u9690\u85cf\u5230\u6258\u76d8");
                    AppendMenuW(menu, MF_STRING, kMenuSettings, L"\u8bbe\u7f6e");
                    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
                    AppendMenuW(menu, MF_STRING, kMenuQuit,
                                L"\u9000\u51fa\u5e76\u6e05\u7406\u7f51\u7edc");
                } else {
                    AppendMenuW(menu, MF_STRING, kMenuShow, L"Show Window");
                    AppendMenuW(menu, MF_STRING, kMenuHide, L"Hide to Tray");
                    AppendMenuW(menu, MF_STRING, kMenuSettings, L"Settings");
                    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
                    AppendMenuW(menu, MF_STRING, kMenuQuit,
                                L"Quit & Clean Up Network");
                }
                SetForegroundWindow(hwnd_);
                TrackPopupMenu(menu, TPM_RIGHTALIGN | TPM_BOTTOMALIGN,
                               pt.x, pt.y, 0, hwnd_, nullptr);
                DestroyMenu(menu);
                return true;
            }
            default:
                return true;
        }
    }
    if (msg == WM_COMMAND) {
        switch (LOWORD(wp)) {
            case kMenuShow: Push(TrayAction::ShowWindow); return true;
            case kMenuHide: Push(TrayAction::HideToTray); return true;
            case kMenuSettings: Push(TrayAction::Settings); return true;
            case kMenuQuit: Push(TrayAction::QuitAndCleanup); return true;
            default: return false;
        }
    }
    return false;
}

LRESULT CALLBACK Tray::IconProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    // Kept for a dedicated tray window if one is ever wanted: the icon only
    // ENQUEUES, the GUI thread consumes via Pump().
    Tray* self = reinterpret_cast<Tray*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (self && self->HandleIconMessage(msg, wp, lp)) return 0;
    return DefWindowProcW(hwnd, msg, wp, lp);
}

HICON MakeBrandIcon() { return DrawBrandIcon(64); }
HICON MakeBrandIconSmall() { return DrawBrandIcon(16); }

}  // namespace linkhelm
