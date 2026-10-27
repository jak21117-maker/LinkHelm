// LinkHelm Engine Supervisor (beta1.0.83)
//
// Why this exists: the Python client occasionally froze on tray-quit and left
// engine processes behind. Root causes: (a) teardown ran synchronously on the
// Tk main thread (taskkill/powershell up to ~7s -> "not responding"); (b) if
// the Python parent died unexpectedly, the lwIP engine kept running (tunnel
// adapter stuck, "occupied" on next connect).
//
// Design (kernel-guaranteed cleanup):
//   - The supervisor owns a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
//     The engine is spawned into that job. If the SUPERVISOR dies for ANY
//     reason, the kernel closes the job handle and every engine process dies.
//   - The supervisor is a child of the Python client and serves one named
//     pipe connection. If the pipe breaks (Python died), the supervisor kills
//     the job and exits. So: Python dead -> supervisor sees EOF -> engines
//     die; supervisor dead -> kernel kills engines. No orphan is possible.
//
// Pipe protocol (UTF-8, one request line -> one response line):
//   START "<exe>" <arg>...   -> OK <pid> | ERR <message>
//   STOP                     -> OK          (TerminateJobObject)
//   STATUS                   -> RUNNING <pid> | IDLE
//   EXIT                     -> OK, then the whole job dies and we exit
//
// Build: mingw g++ -Os -static (see cpp/CMakeLists.txt notes); no CRT bloat.

#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601

#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <wchar.h>

namespace {

const wchar_t* kPipeName = L"\\\\.\\pipe\\linkhelm_supervisor";

void WriteLine(HANDLE h, const char* line) {
    DWORD written = 0;
    DWORD len = (DWORD)lstrlenA(line);
    WriteFile(h, line, len, &written, NULL);
    WriteFile(h, "\n", 1, &written, NULL);
    FlushFileBuffers(h);
}

bool ReadLine(HANDLE h, char* buf, DWORD cap) {
    DWORD n = 0;
    buf[0] = 0;
    while (n + 1 < cap) {
        char c = 0;
        DWORD got = 0;
        if (!ReadFile(h, &c, 1, &got, NULL) || got == 0) return false;  // EOF/error: client gone
        if (c == '\n') break;
        if (c != '\r') buf[n++] = c;
    }
    buf[n] = 0;
    return true;
}

// Parse `START "C:\path with space\hev.exe" arg1 arg2` into argv.
// The exe path MUST be quoted; args are raw tokens (never contain spaces in
// our launcher: a single config path argument).
int ParseStart(const char* line, wchar_t exe[MAX_PATH], char args[8][256]) {
    // skip "START"
    const char* p = line;
    while (*p && *p != ' ') ++p;
    while (*p == ' ') ++p;
    if (*p != '"') return -1;
    ++p;
    char narrow[MAX_PATH];
    int n = 0;
    while (*p && *p != '"' && n + 1 < MAX_PATH) narrow[n++] = *p++;
    if (*p != '"') return -1;
    narrow[n] = 0;
    ++p;
    MultiByteToWideChar(CP_UTF8, 0, narrow, -1, exe, MAX_PATH);
    int argc = 0;
    while (*p == ' ') ++p;
    while (*p && argc < 8) {
        int a = 0;
        while (*p && *p != ' ' && a + 1 < 256) args[argc][a++] = *p++;
        args[argc][a] = 0;
        if (a) ++argc;
        while (*p == ' ') ++p;
    }
    return argc;
}

struct State {
    HANDLE job = nullptr;       // kill-on-close job
    HANDLE engine = nullptr;    // process handle
    HANDLE engineOut = nullptr; // log file for engine stdout/stderr
    DWORD pid = 0;
};

void KillJob(State& st) {
    if (st.job) {
        // NOTE: the supervisor itself is NOT in the job (see wmain). Closing
        // the job handle triggers KILL_ON_JOB_CLOSE for the engines only.
        CloseHandle(st.job);
        st.job = nullptr;
    }
    if (st.engine) { CloseHandle(st.engine); st.engine = nullptr; }
    if (st.engineOut) { CloseHandle(st.engineOut); st.engineOut = nullptr; }
    st.pid = 0;
}

}  // namespace

int wmain() {
    // Single-instance mutex: a stale supervisor from a crashed run must not
    // steal the pipe connection of the new one; the new one just fails to
    // take the mutex and exits 2 (Python retries/times out -> falls back).
    HANDLE mutex = CreateMutexW(NULL, TRUE, L"Local\\LinkHelmSupervisorSingleton");
    if (!mutex || GetLastError() == ERROR_ALREADY_EXISTS) { ; return 2; }
    ;

    // Job object: engines die with us, guaranteed by the kernel.
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jli{};
    jli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    State st;
    st.job = CreateJobObjectW(NULL, NULL);
    if (!st.job) { ; return 3; }
    ;
    SetInformationJobObject(st.job, JobObjectExtendedLimitInformation,
                            &jli, sizeof(jli));
    // The supervisor itself joins the job too: if anything kills us the job
    // handle closes and the engines go with it.
    // (supervisor NOT in job: KILL_ON_JOB_CLOSE fires when OUR handle closes on death)
    ;

    for (;;) {
        HANDLE pipe = CreateNamedPipeW(
            L"\\\\.\\pipe\\linkhelm_supervisor", PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, 4096, 4096, 0, NULL);
        if (pipe == INVALID_HANDLE_VALUE) {
            (void)GetLastError();
            return 4;
        }
        ;

        ConnectNamedPipe(pipe, NULL);  // blocks until the client connects

        // Serve this connection until it breaks; then kill engines (the
        // Python parent is gone) and loop back / exit.
        bool clientGone = false;
        char line[4096];
        while (ReadLine(pipe, line, sizeof(line))) {
            if (lstrcmpiA(line, "STOP") == 0) {
                KillJob(st);
                // Recreate the job so a START can follow a STOP.
                st.job = CreateJobObjectW(NULL, NULL);
                if (st.job) {
                    SetInformationJobObject(
                        st.job, JobObjectExtendedLimitInformation,
                        &jli, sizeof(jli));
    // (supervisor NOT in job: KILL_ON_JOB_CLOSE fires when OUR handle closes on death)
                }
                WriteLine(pipe, "OK");
            } else if (lstrcmpiA(line, "STATUS") == 0) {
                if (st.engine && WaitForSingleObject(st.engine, 0) == WAIT_OBJECT_0) {
                    // engine exited by itself (crash)
                    CloseHandle(st.engine); st.engine = nullptr; st.pid = 0;
                    if (st.engineOut) { CloseHandle(st.engineOut); st.engineOut = nullptr; }
                }
                char out[64];
                if (st.pid) wsprintfA(out, "RUNNING %lu", st.pid);
                else lstrcpyA(out, "IDLE");
                WriteLine(pipe, out);
            } else if (lstrcmpiA(line, "EXIT") == 0) {
                WriteLine(pipe, "OK");
                FlushFileBuffers(pipe);
                DisconnectNamedPipe(pipe);
                CloseHandle(pipe);
                KillJob(st);   // engines die first...
                ReleaseMutex(mutex);
                ExitProcess(0);  // ...then us (job KILL_ON_CLOSE covers the rest)
            } else if (lstrcmpiA(line, "PING") == 0) {
                WriteLine(pipe, "PONG");
            } else if (lstrlenA(line) > 6 && _strnicmp(line, "START ", 6) == 0) {
                wchar_t exe[MAX_PATH];
                char args[8][256];
                int argc = ParseStart(line, exe, args);
                if (argc < 0) { WriteLine(pipe, "ERR bad START syntax"); continue; }
                if (st.pid) { WriteLine(pipe, "ERR already running"); continue; }

                // Engine stdout/stderr -> per-user log file (the old Python
                // reader threads are gone in C++ mode; the log keeps the
                // diagnostics that _read_engine_output used to annotate).
                wchar_t logPath[MAX_PATH];
                wchar_t localAppData[MAX_PATH];
                if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, localAppData))) {
                    wsprintfW(logPath, L"%s\\LinkHelm\\engine", localAppData);
                    CreateDirectoryW(logPath, NULL);
                    lstrcatW(logPath, L"\\engine_stdout.log");
                    st.engineOut = CreateFileW(logPath, GENERIC_WRITE,
                                               FILE_SHARE_READ, NULL,
                                               OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
                    if (st.engineOut != INVALID_HANDLE_VALUE) {
                        SetFilePointer(st.engineOut, 0, NULL, FILE_END);
                    } else {
                        st.engineOut = nullptr;
                    }
                }

                // Build the command line: "<exe>" arg1 arg2 ...
                wchar_t cmd[2048] = L"\"";
                lstrcatW(cmd, exe);
                lstrcatW(cmd, L"\"");
                for (int i = 0; i < argc; ++i) {
                    wchar_t warg[256];
                    MultiByteToWideChar(CP_UTF8, 0, args[i], -1, warg, 256);
                    lstrcatW(cmd, L" ");
                    lstrcatW(cmd, warg);
                }

                STARTUPINFOW si{}; si.cb = sizeof(si);
                si.dwFlags = STARTF_USESTDHANDLES;
                si.hStdOutput = st.engineOut;   // may be NULL -> engine output dropped
                si.hStdError = st.engineOut;
                PROCESS_INFORMATION pi{};
                // CREATE_SUSPENDED so the engine cannot slip out of the job.
                BOOL ok = CreateProcessW(NULL, cmd, NULL, NULL, TRUE,
                                         CREATE_SUSPENDED | CREATE_NO_WINDOW,
                                         NULL, NULL, &si, &pi);
                if (!ok) {
                    char msg[128];
                    wsprintfA(msg, "ERR CreateProcess %lu", GetLastError());
                    WriteLine(pipe, msg);
                    if (st.engineOut) { CloseHandle(st.engineOut); st.engineOut = nullptr; }
                    continue;
                }
                AssignProcessToJobObject(st.job, pi.hProcess);
                ResumeThread(pi.hThread);
                CloseHandle(pi.hThread);
                st.engine = pi.hProcess;
                st.pid = pi.dwProcessId;
                char msg[64];
                wsprintfA(msg, "OK %lu", st.pid);
                WriteLine(pipe, msg);
            } else {
                WriteLine(pipe, "ERR unknown command");
            }
        }
        // Pipe broke: the Python parent is gone. Kill engines, close, and
        // wait for a possible new client (the client may relaunch us anyway).
        clientGone = true;
        (void)clientGone;
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        KillJob(st);
        st.job = CreateJobObjectW(NULL, NULL);
        if (st.job) {
            SetInformationJobObject(st.job, JobObjectExtendedLimitInformation,
                                    &jli, sizeof(jli));
    // (supervisor NOT in job: KILL_ON_JOB_CLOSE fires when OUR handle closes on death)
        }
    }
}
