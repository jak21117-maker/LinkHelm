# build-lwip.ps1 — Windows 入口：调用 MSYS2 MinGW64 执行 ci/build-lwip.sh，
# 把原生 C/C++ 的 lwIP 引擎 (hev-socks5-tunnel.exe) 编译到 laptop_proxy/engine/。
#
# 前置：已安装 MSYS2，且已执行（在 MSYS2 终端）：
#   pacman -S mingw-w64-x86_64-toolchain git mingw-w64-x86_64-meson mingw-w64-x86_64-ninja
#
# 用法（PowerShell，以"管理员"或当前用户均可，需要把 msys2 加入 PATH）：
#   .\ci\build-lwip.ps1
#
# 注意：本机若没有 MSYS2 / 无法访问 GitHub，则无法在此编译；脚本只负责产出
# 原生二进制，编译失败不会改动任何已发布文件。

$ErrorActionPreference = "Stop"

$Msys2 = "C:\msys64\msys2_shell.cmd"
if (-not (Test-Path $Msys2)) {
    # 常见备选路径
    $Msys2 = "C:\tools\msys64\msys2_shell.cmd"
}
if (-not (Test-Path $Msys2)) {
    Write-Error "未找到 MSYS2（期望 C:\msys64\msys2_shell.cmd）。请先安装 MSYS2 并装好 mingw64 工具链。"
    exit 1
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Write-Host "[build-lwip] Repo root: $RepoRoot"

# 在 MinGW64 环境下运行 build-lwip.sh
& $Msys2 -mingw64 -no-start -c "cd '$(($RepoRoot -replace '\\','/'))' && bash ci/build-lwip.sh"
if ($LASTEXITCODE -ne 0) {
    Write-Error "build-lwip.sh 返回非零，编译未成功。请检查上方日志；不要将半成品二进制视为可用。"
    exit $LASTEXITCODE
}

$Out = Join-Path $RepoRoot "laptop_proxy/engine/hev-socks5-tunnel.exe"
if (Test-Path $Out) {
    Write-Host "[build-lwip] 成功产出: $Out"
} else {
    Write-Error "未生成预期产物: $Out"
    exit 1
}
