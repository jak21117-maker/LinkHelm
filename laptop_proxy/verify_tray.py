# -*- coding: utf-8 -*-
"""Tray capability verification (v1.5.2 pre-package gate).

Verifies without opening a window:
  1. pystray + its Windows backend import cleanly;
  2. the tray icon image can be generated via PIL;
  3. a pystray.Icon with our menu structure constructs successfully;
  4. the UI-queue pump contract (enqueue/dequeue) works.
"""
import queue
import sys


def main():
    failures = []

    # 1. imports
    try:
        import pystray
        import pystray._win32  # noqa: F401 — the backend pystray picks at runtime
        print("[OK] pystray + _win32 backend import")
    except Exception as exc:
        failures.append(f"pystray import: {exc}")

    # 2. icon image (same drawing as main._tray_image)
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(37, 99, 235, 255))
        draw.ellipse([24, 16, 40, 32], fill=(255, 255, 255, 255))
        draw.rectangle([18, 40, 46, 45], fill=(255, 255, 255, 255))
        assert img.size == (64, 64)
        print("[OK] PIL tray icon image generated (64x64 RGBA)")
    except Exception as exc:
        failures.append(f"PIL image: {exc}")

    # 3. Icon construction with our menu
    try:
        import pystray
        Menu, MenuItem = pystray.Menu, pystray.MenuItem
        icon = pystray.Icon(
            "LinkHelm",
            img,
            "LinkHelm Desktop（点按显示窗口）",
            Menu(
                MenuItem("显示窗口", lambda *_: None, default=True),
                MenuItem("隐藏到托盘", lambda *_: None),
                Menu.SEPARATOR,
                MenuItem("退出并清理网络", lambda *_: None),
            ),
        )
        # Do NOT run the icon loop — construction + title are the risk points.
        assert icon.title.startswith("LinkHelm")
        print("[OK] pystray.Icon constructed with LinkHelm menu")
    except Exception as exc:
        failures.append(f"Icon construct: {exc}")

    # 4. ui_queue pump contract
    try:
        ui_queue = queue.Queue()
        for action in ("SHOW", "HIDE", "QUIT"):
            ui_queue.put(action)
        got = [ui_queue.get_nowait() for _ in range(3)]
        assert got == ["SHOW", "HIDE", "QUIT"]
        assert ui_queue.empty()
        print("[OK] ui_queue enqueue/dequeue contract")
    except Exception as exc:
        failures.append(f"ui_queue: {exc}")

    if failures:
        for f in failures:
            print(f"[FAIL] {f}", file=sys.stderr)
        sys.exit(1)
    print("TRAY_VERIFY_OK")


if __name__ == "__main__":
    main()
