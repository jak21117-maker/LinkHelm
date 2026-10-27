# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('customtkinter')

# beta1.0.4: native C/C++ lwIP engine. We ship the MAINTAINER'S official
# Windows build (releases/download/2.19.1/hev-socks5-tunnel-win64.zip):
# hev-socks5-tunnel.exe + wintun.dll + msys-2.0.dll (the MSYS2 runtime the
# official exe is linked against — must ride along or the exe won't start).
# Optional on purpose: absent until dropped into laptop_proxy/engine/, then
# packed automatically so this very spec produces the lwIP-default package.
# beta1.0.83: linkhelm_supervisor.exe — the C++ engine lifecycle supervisor
# (Job Object cleanup, named-pipe control). See cpp/src/supervisor.cpp.
_lwip_binaries = [
    ('engine/' + name, 'engine')
    for name in ('hev-socks5-tunnel.exe', 'wintun.dll', 'msys-2.0.dll',
                 'linkhelm_supervisor.exe', 'linkhelm_netops.exe',
                 'linkhelm_ics.exe')
    if os.path.isfile('engine/' + name)
]

# beta1.0.81: store accelerator kernel — the OFFICIAL mihomo (Clash Meta)
# core, downloaded straight from the upstream MetaCubeX/mihomo GitHub
# release v1.19.33 (the same open-source core the Clash Verge client ships;
# SHA256 pinned in store_accel.MIHOMO_SHA256). Shipped as a SEPARATE
# process file (GPL does not extend to LinkHelm; see THIRD_PARTY_NOTICES.md
# for the required source link). Optional like engine/: packed when present.
_mihomo_datas = (
    [('mihomo/mihomo.exe', 'mihomo')]
    if os.path.isfile('mihomo/mihomo.exe') else []
)

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    # Patch B: ship build-time-verified tun2socks/Wintun binaries with the onedir
    # package so a normal install never downloads anything at runtime.
    # beta1.0.5: tun2socks 已彻底移除，仅打包 lwIP 原生引擎（engine/ 下的
    # hev-socks5-tunnel.exe + wintun.dll + msys-2.0.dll）。
    binaries=[
        *_lwip_binaries,
        *_mihomo_datas,
    ],
    datas=datas,
    # v1.4.93: pystray picks its Windows backend dynamically (pystray._win32)
    # — static analysis can't see it, so both the package and its backend are
    # declared explicitly. Pillow is pulled in by normal analysis.
    # v1.5.47: lane_router removed entirely — no longer a hidden import.
    hiddenimports=['darkdetect', 'customtkinter', 'pystray', 'pystray._win32',
                   'PIL'],
    # v1.5.43: the app never uses numpy (it entered the build venv as an
    # opencv dependency and PyInstaller pulled ~29MB of numpy/numpy.libs
    # into the package). Exclude it and other heavy scientific stacks that
    # can never be part of this GUI app so build-env drift cannot bloat
    # the package again.
    # v1.5.47: stdlib slimming — unittest/pydoc_data and friends are never
    # imported by this app.
    excludes=['numpy', 'cv2', 'matplotlib', 'scipy', 'pandas',
              'unittest', 'pydoc_data', 'test', 'lib2to3', 'tkinter.test',
              'distutils', 'sqlite3', 'xmlrpc'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LinkHelm_Desktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='LinkHelm_Desktop',
)
