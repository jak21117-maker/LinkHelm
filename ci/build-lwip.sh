#!/usr/bin/env bash
# build-lwip.sh — 在 MSYS2 MinGW64 环境下编译 LinkHelm 的 Windows 原生 lwIP 引擎。
#
# 这个脚本产出 hev-socks5-tunnel.exe（C/C++ 编写，内含 lwIP + Wintun 支持），
# 落到 laptop_proxy/engine/，正好被 laptop_proxy/engine.py 的 LwipEngine 拉起。
# 协议栈全部在 C 里，Python 只负责编排（生成配置、启停、Wintun 互斥、清理）。
#
# 用法（在 MSYS2 的 "MinGW64" 终端里）：
#   cd <repo>
#   bash ci/build-lwip.sh
#
# 依赖（pacman -S，首次需联网）：
#   mingw-w64-x86_64-toolchain git mingw-w64-x86_64-meson mingw-w64-x86_64-ninja
#
# 重要：下方提交号必须先 pin 死（任务书 104-B）。构建前请把 HEV_COMMIT / LWIP_COMMIT
# 改成实际锁定的 commit，并记录其 SHA-256 到 DEPENDENCY_LOCK.md。

set -euo pipefail

# ===== 待锁定（104-B）：构建前必须固定 =====
HEV_COMMIT="${HEV_COMMIT:-TODO_PIN_HEV_COMMIT}"
LWIP_COMMIT="${LWIP_COMMIT:-TODO_PIN_LWIP_COMMIT}"
# ==========================================

REPO_URL="https://github.com/heiher/hev-socks5-tunnel"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE_DIR="$ROOT/laptop_proxy/engine"
SRC="$ROOT/.build/lwip-src"
OUT="$ENGINE_DIR/hev-socks5-tunnel.exe"

echo "[build-lwip] 目标: $OUT"

if [[ "$HEV_COMMIT" == TODO* || "$LWIP_COMMIT" == TODO* ]]; then
  echo "[build-lwip] 错误：请先固定 HEV_COMMIT 与 LWIP_COMMIT 再构建（见脚本顶部）。" >&2
  exit 1
fi

# 1) 拉取源码 + 子模块（lwIP 在 third/lwip）
rm -rf "$SRC"
git clone --recursive "$REPO_URL" "$SRC"
cd "$SRC"
git checkout "$HEV_COMMIT"
git submodule update --init --recursive
git -C third/lwip checkout "$LWIP_COMMIT" 2>/dev/null || \
  echo "[build-lwip] 提示：若 lwIP 不是独立 submodule，请按 Hev 实际布局固定其 tree-ish。"

# 2) 编译（Hev 自带 build.sh，走 meson/ninja；MinGW 原生目标）
bash build.sh || meson setup build && ninja -C build

# 3) 取出产物（路径随版本可能变动，回退多路径查找）
BIN=$(find "$SRC" -name 'hev-socks5-tunnel*.exe' -type f | head -n1)
if [[ -z "$BIN" ]]; then
  echo "[build-lwip] 错误：未找到编译产物 hev-socks5-tunnel.exe" >&2
  exit 1
fi

mkdir -p "$ENGINE_DIR"
cp "$BIN" "$OUT"
chmod +x "$OUT"

# 4) 记录 SHA-256（用于 DEPENDENCY_LOCK.md）
SHA=$(sha256sum "$OUT" | cut -d' ' -f1)
echo "[build-lwip] 完成: $OUT"
echo "[build-lwip] SHA-256: $SHA"
echo "[build-lwip] 请将上面 SHA-256 与 HEV_COMMIT=$HEV_COMMIT / LWIP_COMMIT=$LWIP_COMMIT 写入 DEPENDENCY_LOCK.md 的 lwIP 段落。"
