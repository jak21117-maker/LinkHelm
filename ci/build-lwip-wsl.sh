#!/usr/bin/env bash
# ============================================================================
# build-lwip-wsl.sh — 在 WSL (Ubuntu/Debian) 里交叉编译 LinkHelm 的原生
# C/C++ lwIP 引擎 (hev-socks5-tunnel) 为 Windows x86_64 可执行文件。
#
# 用法（在 Windows 终端 PowerShell/cmd，或 WSL 内）:
#   wsl bash /mnt/c/Users/90935/WorkBuddy/2026-10-15-18-57-46/linkhelm_repo/ci/build-lwip-wsl.sh
# 或在 WSL 内:
#   bash /mnt/c/Users/90935/WorkBuddy/2026-10-15-18-57-46/linkhelm_repo/ci/build-lwip-wsl.sh
#
# 产物:
#   laptop_proxy/engine/hev-socks5-tunnel.exe   (lwIP 数据面，默认内核)
#   laptop_proxy/engine/wintun.dll              (引擎同目录，LoadLibrary 依赖)
# 同时打印 SHA-256，请回填 DEPENDENCY_LOCK.md 的 lwIP 段落（任务书 104-B）。
# ============================================================================
set -euo pipefail

HEV_TAG="2.19.1"   # 锁定版本；升级时同步更新 DEPENDENCY_LOCK.md

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_OUT="$SCRIPT_DIR/../laptop_proxy/engine"
mkdir -p "$ENGINE_OUT"

WORK="$(mktemp -d /tmp/hev-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# 1) 代理：Windows 宿主上的 127.0.0.1:7897，WSL2 里要访问宿主 IP。
#    依次尝试 宿主IP、localhost，哪个通 GitHub 用哪个；都不通则直连。
# ---------------------------------------------------------------------------
HOST_IP="$(grep -m1 nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}' || true)"
PICKED=""
for p in "http://${HOST_IP}:7897" "http://127.0.0.1:7897"; do
  [ -z "$HOST_IP" ] && [ "$p" != "http://127.0.0.1:7897" ] && continue
  if curl -s -m 5 -o /dev/null -x "$p" https://github.com; then
    PICKED="$p"; break
  fi
done
if [ -n "$PICKED" ]; then
  echo "[proxy] using $PICKED"
  export http_proxy="$PICKED" https_proxy="$PICKED" HTTP_PROXY="$PICKED" HTTPS_PROXY="$PICKED"
else
  echo "[proxy] none reachable; going direct"
fi

# ---------------------------------------------------------------------------
# 2) 依赖：git/make + mingw-w64 交叉工具链（apt 走清华镜像加速）。
# ---------------------------------------------------------------------------
if ! command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo sed -i.bak \
      -e 's|archive.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' \
      -e 's|security.ubuntu.com|mirrors.tuna.tsinghua.edu.cn|g' \
      /etc/apt/sources.list 2>/dev/null || true
    sudo apt-get update -y
    sudo apt-get install -y git make gcc-mingw-w64-x86-64
  else
    echo "ERROR: x86_64-w64-mingw32-gcc missing and apt-get unavailable." >&2
    echo "Install manually: sudo apt install gcc-mingw-w64-x86-64" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 3) 拉取 Hev 源码（含 lwIP/wintun/yaml/hev-task-system 全部子模块）。
# ---------------------------------------------------------------------------
cd "$WORK"
git clone --depth 1 --branch "$HEV_TAG" --recursive \
  https://github.com/heiher/hev-socks5-tunnel
cd hev-socks5-tunnel
echo "[hev] checked out $HEV_TAG @ $(git rev-parse HEAD)"

# ---------------------------------------------------------------------------
# 4) 交叉编译。要点：
#    - CROSS_PREFIX= 让 Makefile 全链（主程序+三个第三方）都换 mingw 工具链；
#    - 非 MSYS 环境下 Makefile 不会追加 Windows 系统库，用 LFLAGS 手动补；
#    - 交叉 gcc 版本可能触发新的 -Werror 警告：严格失败时自动带
#      CFLAGS=-Wno-error 重试一次。
# ---------------------------------------------------------------------------
MAKE_ARGS=(CROSS_PREFIX=x86_64-w64-mingw32- LFLAGS="-lws2_32 -lIphlpapi -lole32 -luuid")
if ! make "${MAKE_ARGS[@]}"; then
  echo "[make] strict build failed; retrying with -Wno-error"
  make clean >/dev/null 2>&1 || true
  make "${MAKE_ARGS[@]}" CFLAGS=-Wno-error
fi

[ -f bin/hev-socks5-tunnel.exe ] || { echo "ERROR: exe not produced" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 5) 产物落位 + SHA-256 回填依据。
# ---------------------------------------------------------------------------
cp bin/hev-socks5-tunnel.exe "$ENGINE_OUT/"

WT="$(find third-part/wintun -iname 'wintun.dll' | grep -i 'amd64\|x64\|x86_64' | head -1 || true)"
[ -z "$WT" ] && WT="$(find third-part/wintun -iname 'wintun.dll' | head -1 || true)"
if [ -n "$WT" ]; then
  cp "$WT" "$ENGINE_OUT/"
else
  echo "[warn] wintun.dll not found in third-part/wintun — engine dir keeps the one from bundled_bin" >&2
fi

echo ""
echo "=== DONE: lwIP engine artifacts ==="
ls -la "$ENGINE_OUT"
echo ""
echo "=== SHA-256 (paste into DEPENDENCY_LOCK.md) ==="
sha256sum "$ENGINE_OUT"/hev-socks5-tunnel.exe "$ENGINE_OUT"/wintun.dll 2>/dev/null || true
echo ""
echo "hev commit: $(git rev-parse HEAD)  (tag $HEV_TAG)"
echo "Next: restart LinkHelm Desktop — it will auto-select the lwIP kernel."
