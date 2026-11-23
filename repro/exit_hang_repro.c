// exit_hang_repro: tiny CRT-free Windows programs that do (or do not) hang at exit.
//
// Each mode leaves the process in a known state when the entry point returns with a plain
// `return` (no ExitProcess). On Windows, returning from the entry point of an executable
// built without the C runtime ends only the initial thread; the process ends when its last
// thread ends. These modes are deterministic controls for win-exit-hang-probe.
//
// usage: exit_hang_repro.exe MODE      (MODE is a single digit)
//   0  default thread pool, the worker is idle when the entry returns    -> exits (control)
//   1  default thread pool, the worker is still inside a 3 s callback    -> hangs
//   2  private thread pool with a minimum of 2 threads, workers idle     -> hangs
//   3  ordinary thread sleeping 3 s                                      -> exits after ~3 s,
//                                                                          exit code 0
//   4  starts a child that inherits stdout/stderr, then calls
//      ExitProcess: the process is gone but the pipes stay open ~5 s    -> "pipe held" case
//   9  (internal) the child used by mode 4: sleeps 5 s, then exits
//
// In modes 0-3 the entry returns 0x60d, so a different exit code shows which thread ended
// the process last. Mode 4 calls ExitProcess(0x60d); mode 9 calls ExitProcess(0).
// Started by hand from a console, modes 1 and 2 stay running until you close them (that is
// the point: they are the hangs).
//
// Build (from a "x64 Native Tools Command Prompt"; build.cmd does the same and creates
// build\ first):
//   cl /nologo /O1 /GS- /W4 repro\exit_hang_repro.c /Fobuild\ /Febuild\exit_hang_repro.exe
//      /link /ENTRY:start /NODEFAULTLIB /SUBSYSTEM:CONSOLE kernel32.lib
#include <windows.h>

#define ENTRY_EXIT_CODE 0x60d

static void CALLBACK work(PTP_CALLBACK_INSTANCE instance, void* context)
{
    (void)instance;
    if (context)
        Sleep(3000);
}

static DWORD WINAPI sleeper(void* context)
{
    (void)context;
    Sleep(3000);
    return 0;
}

static void write_out(const char* text)
{
    DWORD length = 0, written = 0;
    while (text[length])
        ++length;
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), text, length, &written, NULL);
}

// The last decimal digit on the command line, or -1.
static int mode_from_command_line(void)
{
    const char* p = GetCommandLineA();
    int mode = -1;
    for (; *p; ++p)
        if (*p >= '0' && *p <= '9' && (p[1] == '\0' || p[1] == ' ' || p[1] == '\t'))
            mode = *p - '0';
    return mode;
}

static void start_inheriting_child(void)
{
    char path[MAX_PATH + 16];
    DWORD n = GetModuleFileNameA(NULL, path + 1, MAX_PATH);
    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    char* d;
    if (n == 0 || n >= MAX_PATH)
        return;
    path[0] = '"';
    d = path + 1 + n;
    *d++ = '"';
    *d++ = ' ';
    *d++ = '9';
    *d = '\0';
    SecureZeroMemory(&si, sizeof(si));  // inline, no C runtime needed
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (CreateProcessA(NULL, path, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
}

DWORD start(void)
{
    int mode = mode_from_command_line();
    if (mode == 0) {
        TrySubmitThreadpoolCallback(work, NULL, NULL);
    } else if (mode == 1) {
        TrySubmitThreadpoolCallback(work, (void*)1, NULL);
    } else if (mode == 2) {
        PTP_POOL pool = CreateThreadpool(NULL);
        TP_CALLBACK_ENVIRON env;
        InitializeThreadpoolEnvironment(&env);
        SetThreadpoolCallbackPool(&env, pool);
        SetThreadpoolThreadMaximum(pool, 4);
        SetThreadpoolThreadMinimum(pool, 2);
        TrySubmitThreadpoolCallback(work, NULL, &env);
    } else if (mode == 3) {
        CloseHandle(CreateThread(NULL, 0, sleeper, NULL, 0, NULL));
    } else if (mode == 4) {
        start_inheriting_child();
        ExitProcess(ENTRY_EXIT_CODE);
    } else if (mode == 9) {
        Sleep(5000);
        ExitProcess(0);
    } else {
        write_out("usage: exit_hang_repro.exe 0|1|2|3|4\r\n");
        ExitProcess(2);
    }
    Sleep(50);
    return ENTRY_EXIT_CODE;  // a plain return, no ExitProcess
}
