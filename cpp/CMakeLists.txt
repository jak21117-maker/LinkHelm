# LinkHelm Desktop — native C++ build (C++ rewrite P1/P2).
# Toolchain: the portable MinGW-w64 unpacked at ../toolchain/mingw64
# (winlibs build; no system-wide install needed).
#
#   cmake -S . -B build -G "MinGW Makefiles" ^
#         -DCMAKE_CXX_COMPILER=../toolchain/mingw64/bin/g++.exe
#   cmake --build build --parallel
cmake_minimum_required(VERSION 3.16)
project(LinkHelmDesktop LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_CXX_COMPILER)
    set(CMAKE_CXX_COMPILER "${CMAKE_CURRENT_SOURCE_DIR}/../toolchain/mingw64/bin/g++.exe")
endif()

add_executable(LinkHelmDesktop WIN32
    src/app_main.cpp
    src/accel_controller.cpp
    src/net_control.cpp
    src/relay.cpp
    src/subscription.cpp
    src/tray.cpp
    src/file_transfer.cpp
    src/ft_receiver.cpp
    src/tun_manager.cpp
)

target_include_directories(LinkHelmDesktop PRIVATE src)
target_compile_definitions(LinkHelmDesktop PRIVATE
    UNICODE _UNICODE _WIN32_WINNT=0x0601 WIN32_LEAN_AND_MEAN)

# beta1.0.8 size pass (user directive): no RTTI, no C++ exceptions (the two
# std::stoi/stoll call sites were rewritten to strtol first), -Os, stripped,
# section GC. With static libstdc++ these switches bring the exe back near
# plain-C size.
target_compile_options(LinkHelmDesktop PRIVATE
    -fno-rtti -fno-exceptions -Os -ffunction-sections -fdata-sections)

target_link_libraries(LinkHelmDesktop PRIVATE
    winhttp iphlpapi shlwapi advapi32 comctl32 shell32 user32 gdi32 ws2_32
    wsock32 ole32 oleaut32 comdlg32)

# MinGW needs -municode to pick wWinMain as the entry point (GUI subsystem).
# -static*: the shipped exe must not depend on libgcc/libstdc++/winpthread
# DLLs being present next to it (a bare exe crashed with 0xC0000135).
# -mwindows forces GUI subsystem: without it the exe is CONSOLE (subsystem=3)
# and Windows pops a black console window next to the UI (silent-launch fix).
set_target_properties(LinkHelmDesktop PROPERTIES
    LINK_FLAGS "-municode -mwindows -static -static-libgcc -static-libstdc++ -s -Wl,--gc-sections")
