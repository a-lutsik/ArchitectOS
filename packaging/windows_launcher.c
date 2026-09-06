/* Console launcher for the Windows share package.

   Next to this .exe there must be:
     runtime/python/python.exe   (official embeddable CPython)
     runtime/backend/            (architectos package parent)
     runtime/frontend/           (SPA)

   architectos-server.exe  →  python -m architectos <args>
   architectos-mcp.exe     →  python -m architectos mcp <args>
*/
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static void die(const wchar_t *msg) {
    fwprintf(stderr, L"architectos: %s\n", msg);
    ExitProcess(1);
}

static int name_has_mcp(const wchar_t *name) {
    const wchar_t *p;
    for (p = name; *p; p++) {
        if ((p[0] == L'm' || p[0] == L'M') && (p[1] == L'c' || p[1] == L'C') && (p[2] == L'p' || p[2] == L'P')) {
            return 1;
        }
    }
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    wchar_t exe[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, exe, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        die(L"GetModuleFileName failed");
    }

    wchar_t *slash = wcsrchr(exe, L'\\');
    if (!slash) {
        slash = wcsrchr(exe, L'/');
    }
    if (!slash) {
        die(L"cannot parse executable path");
    }
    *slash = 0;
    wchar_t *exedir = exe;
    wchar_t *exename = slash + 1;
    int mcp = name_has_mcp(exename);

    wchar_t python[MAX_PATH];
    if (_snwprintf(python, MAX_PATH, L"%s\\runtime\\python\\python.exe", exedir) < 0) {
        die(L"path too long");
    }
    python[MAX_PATH - 1] = 0;
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        die(L"runtime\\python\\python.exe not found next to this executable");
    }

    wchar_t frontend[MAX_PATH];
    wchar_t pyhome[MAX_PATH];
    _snwprintf(frontend, MAX_PATH, L"%s\\runtime\\frontend", exedir);
    _snwprintf(pyhome, MAX_PATH, L"%s\\runtime\\python", exedir);
    frontend[MAX_PATH - 1] = 0;
    pyhome[MAX_PATH - 1] = 0;

    SetEnvironmentVariableW(L"PYTHONHOME", pyhome);
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    SetEnvironmentVariableW(L"ARCHITECTOS_FRONTEND", frontend);

    wchar_t pathbuf[32768];
    DWORD path_len = GetEnvironmentVariableW(L"PATH", pathbuf, 32768);
    if (path_len == 0 || path_len >= 32768) {
        SetEnvironmentVariableW(L"PATH", pyhome);
    } else {
        wchar_t newpath[32768];
        if (_snwprintf(newpath, 32768, L"%s;%s", pyhome, pathbuf) < 0) {
            die(L"PATH too long");
        }
        newpath[32767] = 0;
        SetEnvironmentVariableW(L"PATH", newpath);
    }

    wchar_t cmdline[32768];
    wchar_t *p = cmdline;
    int cap = 32768;
    int wrote = _snwprintf(p, cap, L"\"%s\" -m architectos", python);
    if (wrote < 0) {
        die(L"command line too long");
    }
    p += wrote;
    cap -= wrote;
    if (mcp) {
        wrote = _snwprintf(p, cap, L" mcp");
        if (wrote < 0) {
            die(L"command line too long");
        }
        p += wrote;
        cap -= wrote;
    }
    for (int i = 1; i < argc; i++) {
        wrote = _snwprintf(p, cap, L" \"%s\"", argv[i]);
        if (wrote < 0) {
            die(L"command line too long");
        }
        p += wrote;
        cap -= wrote;
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(python, cmdline, NULL, NULL, TRUE, 0, NULL, exedir, &si, &pi)) {
        fwprintf(stderr, L"architectos: failed to start Python (error %lu)\n", GetLastError());
        return 1;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
