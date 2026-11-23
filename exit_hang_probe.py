"""win-exit-hang-probe: why doesn't a Windows process exit?

Launches a program many times (or looks at one that is already running) and, when it does not
finish in time, records what is still keeping it alive before anything is killed:

  - whether the process object is signaled (has the process really ended?)
  - whether its stdout/stderr pipes reached end-of-file (does someone else still hold them?)
  - every thread still in the process: start address as module+offset, whether it is the
    initial thread or a default thread-pool worker (ntdll TppWorkerThread), CPU time, age
  - the loaded modules

Then it keeps watching for --linger seconds to see whether the process ends by itself, and
terminates it only if it is still alive. Standard library only, Windows, Python 3.9+.

usage:
  python exit_hang_probe.py [options] -- PROGRAM [ARGS...]
  python exit_hang_probe.py --pid PID
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W

__version__ = "1.0.0"

if os.name != "nt":
    sys.exit("win-exit-hang-probe runs on Windows only")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ThreadID", W.DWORD),
                ("th32OwnerProcessID", W.DWORD), ("tpBasePri", W.LONG), ("tpDeltaPri", W.LONG),
                ("dwFlags", W.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", W.DWORD),
                ("EntryPoint", ctypes.c_void_p)]


U64P = ctypes.POINTER(ctypes.c_uint64)
k32.CreateToolhelp32Snapshot.restype = W.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
k32.Thread32First.argtypes = [W.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.argtypes = [W.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.OpenThread.restype = W.HANDLE
k32.OpenThread.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.OpenProcess.restype = W.HANDLE
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k32.GetCurrentProcess.restype = W.HANDLE
k32.GetCurrentThreadId.restype = W.DWORD
k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
k32.GetThreadTimes.argtypes = [W.HANDLE, U64P, U64P, U64P, U64P]
k32.GetProcessTimes.argtypes = [W.HANDLE, U64P, U64P, U64P, U64P]
k32.GetExitCodeThread.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
k32.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
k32.CloseHandle.argtypes = [W.HANDLE]
ntdll.NtQueryInformationThread.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.ULONG,
                                           ctypes.c_void_p]
psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.POINTER(W.HMODULE), W.DWORD,
                                       ctypes.POINTER(W.DWORD), W.DWORD]
psapi.GetModuleBaseNameW.argtypes = [W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD]
psapi.GetModuleInformation.argtypes = [W.HANDLE, W.HMODULE, ctypes.POINTER(MODULEINFO), W.DWORD]

THREAD_QUERY_INFORMATION, SYNCHRONIZE = 0x40, 0x100000
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ, PROCESS_TERMINATE = 0x400, 0x10, 0x1
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPTHREAD, LIST_MODULES_ALL, THREAD_QUERY_WIN32_START_ADDRESS = 0x4, 0x3, 9
BELOW_NORMAL_PRIORITY_CLASS, STILL_ACTIVE = 0x4000, 0x103
CRT_MODULES = ("ucrtbase", "msvcrt", "msvcr", "vcruntime", "msvcp")
IGNORED_CHILDREN = {"conhost.exe"}  # the console host every console program gets


# ---------------------------------------------------------------- process and thread facts

def thread_ids(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry, ids = THREADENTRY32(), []
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Thread32First(snap, ctypes.byref(entry))
    while ok:
        if entry.th32OwnerProcessID == pid:
            ids.append(entry.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return ids


def children(pid):
    """Live processes whose parent process ID is pid: [{"pid", "name"}]. Parent IDs are not
    reference-counted by Windows, so treat a match as a lead, not proof."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry, out = PROCESSENTRY32W(), []
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if (entry.th32ParentProcessID == pid and entry.th32ProcessID != pid
                and entry.szExeFile.lower() not in IGNORED_CHILDREN):
            out.append({"pid": entry.th32ProcessID, "name": entry.szExeFile})
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def start_address(thread_handle):
    addr = ctypes.c_void_p()
    ntdll.NtQueryInformationThread(thread_handle, THREAD_QUERY_WIN32_START_ADDRESS,
                                   ctypes.byref(addr), ctypes.sizeof(addr), None)
    return addr.value or 0


def modules(proc_handle):
    """[(name, base, size, entry_point)] of every module, first one is the executable."""
    need, count = W.DWORD(), 256
    while True:
        arr = (W.HMODULE * count)()
        if not psapi.EnumProcessModulesEx(proc_handle, arr, ctypes.sizeof(arr),
                                          ctypes.byref(need), LIST_MODULES_ALL):
            return []
        if need.value <= ctypes.sizeof(arr):
            break
        count = need.value // ctypes.sizeof(W.HMODULE) + 16
    mods, name = [], ctypes.create_unicode_buffer(260)
    for i in range(need.value // ctypes.sizeof(W.HMODULE)):
        info = MODULEINFO()
        psapi.GetModuleInformation(proc_handle, arr[i], ctypes.byref(info), ctypes.sizeof(info))
        psapi.GetModuleBaseNameW(proc_handle, arr[i], name, 260)
        mods.append((name.value, info.lpBaseOfDll or 0, info.SizeOfImage, info.EntryPoint or 0))
    return mods


def tpp_worker_offsets():
    """ntdll offsets where default thread-pool workers start (TppWorkerThread), learnt by
    queueing a callback in this process and reading the start address of the thread that
    runs it. ntdll is mapped at the same base in every process of a boot session."""
    seen, done = [], threading.Event()
    cb_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

    def callback(instance, context):
        seen.append(k32.GetCurrentThreadId())
        done.set()

    cb = cb_type(callback)
    if not k32.TrySubmitThreadpoolCallback(cb, None, None) or not done.wait(5):
        return set()
    ntdll_mod = [m for m in modules(k32.GetCurrentProcess()) if m[0].lower() == "ntdll.dll"]
    out = set()
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, seen[0])
    if h and ntdll_mod:
        _, base, size, _ = ntdll_mod[0]
        addr = start_address(h)
        if base <= addr < base + size:
            out.add(addr - base)
    if h:
        k32.CloseHandle(h)
    time.sleep(0.05)  # let the callback return before cb can be collected
    return out


def describe(addr, mods, tpp):
    for name, base, size, _ in mods:
        if base <= addr < base + size:
            return f"{name}+0x{addr - base:x}"
    return hex(addr) if addr else "?"


def filetime_ms(value):
    return round(value / 1e4, 1)


def thread_report(pid, proc_handle, mods, tpp):
    created0, exit0, kt, ut = (ctypes.c_uint64() for _ in range(4))
    k32.GetProcessTimes(proc_handle, created0, exit0, kt, ut)
    exe_entry = mods[0][3] if mods else 0
    ntdll = next((m for m in mods if m[0].lower() == "ntdll.dll"), None)
    rows = []
    for tid in thread_ids(pid):
        h = k32.OpenThread(THREAD_QUERY_INFORMATION | SYNCHRONIZE, False, tid)
        if not h:
            err = ctypes.get_last_error()
            rows.append({"tid": tid, "error": "ended while being inspected" if err == 87 else
                         f"OpenThread failed (error {err})"})
            continue
        c, e, kt, ut = (ctypes.c_uint64() for _ in range(4))
        k32.GetThreadTimes(h, c, e, kt, ut)
        code = W.DWORD()
        k32.GetExitCodeThread(h, ctypes.byref(code))
        addr = start_address(h)
        if exe_entry and addr == exe_entry:
            kind = "initial thread (executable entry point)"
        elif ntdll and addr - ntdll[1] in tpp:
            kind = "thread-pool worker (ntdll TppWorkerThread)"
        elif mods and mods[0][1] <= addr < mods[0][1] + mods[0][2]:
            kind = "thread started by the executable"
        elif addr:
            kind = "thread started by " + describe(addr, mods, tpp).split("+")[0]
        else:
            kind = "unknown start"
        rows.append({"tid": tid, "start": describe(addr, mods, tpp), "kind": kind,
                     "running": k32.WaitForSingleObject(h, 0) != 0,
                     "exit_code": hex(code.value),
                     "age_ms_after_process_start": filetime_ms(c.value - created0.value),
                     "cpu_ms": filetime_ms(kt.value + ut.value)})
        k32.CloseHandle(h)
    return rows


def hints(rec):
    """Plain-language reading of a record. Hints, not proof."""
    out, threads = [], rec.get("threads", [])
    alive = not rec.get("process_signaled", True)
    if not alive:
        if rec.get("stdout_eof") is False or rec.get("stderr_eof") is False:
            who = ", ".join(f"{c['name']} (pid {c['pid']})" for c in rec.get("children", []))
            out.append("the process has ended but its stdout/stderr pipe is still open: another "
                       "process inherited the pipe and still holds it"
                       + (f"; children still running: {who}" if who else
                          "; no child process of it is still running, so look for a grandchild"
                          " or a process that duplicated the handle"))
        return out
    if not threads:
        out.append("no threads are listed but the process object is not signaled; this is "
                   "unusual (protected process, or the process is being torn down)")
        return out
    kinds = [t.get("kind", "") for t in threads]
    if not any(k.startswith("initial thread") for k in kinds):
        out.append("the initial thread is gone: the entry point returned or called ExitThread, "
                   "and the process lives on through its remaining threads")
    else:
        out.append("the initial thread is still running: main() has not returned, or the "
                   "runtime is waiting at shutdown (for example joining a thread that never ends)")
    if kinds and all(k.startswith("thread-pool worker") for k in kinds):
        out.append(f"only thread-pool workers are left ({len(threads)}); an entry point that "
                   "returns instead of calling ExitProcess waits for them. Who queued the work is "
                   "not visible here")
    for k in sorted(set(kinds)):
        if not k.startswith("thread started by") or k.endswith("executable"):
            continue
        module = k.rsplit(" ", 1)[-1].lower()
        if module.startswith(CRT_MODULES):
            out.append(f"some threads were {k[7:]}, the C runtime's thread start "
                       "(_beginthreadex, std::thread, language runtimes): the program created "
                       "them, and the real thread function is not visible from the start address")
        else:
            out.append(f"some threads were {k[7:]}: check what that module waits for")
    if any(k == "thread started by the executable" for k in kinds):
        out.append("threads of the program's own code are still running")
    return out


# ---------------------------------------------------------------- launching

def launch(cmd, stdin_bytes, timeout, priority, linger, tpp):
    flags = BELOW_NORMAL_PRIORITY_CLASS if priority == "below" else 0
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)
    got = {"stdout": bytearray(), "stderr": bytearray()}
    eof = {"stdout": threading.Event(), "stderr": threading.Event()}

    def pump(name, stream):
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            got[name] += chunk
        eof[name].set()

    for n in got:
        threading.Thread(target=pump, args=(n, getattr(proc, n)), daemon=True).start()
    try:
        if stdin_bytes:
            proc.stdin.write(stdin_bytes)
        proc.stdin.close()
    except OSError:
        pass
    h = int(proc._handle)  # Popen keeps a full-access process handle
    deadline = t0 + timeout
    finished = k32.WaitForSingleObject(h, max(0, int(timeout * 1000))) == 0
    finished = finished and all(e.wait(max(0.0, deadline - time.perf_counter())) for e in
                                eof.values())
    if finished:
        proc.wait()
        return {"ok": True, "exit_code": proc.returncode,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
    mods = modules(h)
    signaled = k32.WaitForSingleObject(h, 0) == 0
    rec = {"ok": False, "pid": proc.pid, "timeout_s": timeout, "process_signaled": signaled,
           "threads": thread_report(proc.pid, h, mods, tpp),
           "children": children(proc.pid),
           "stdout_eof": eof["stdout"].is_set(), "stderr_eof": eof["stderr"].is_set(),
           "stdout_bytes": len(got["stdout"]), "stderr_bytes": len(got["stderr"]),
           "modules": [m[0] for m in mods],
           "inspected_after_s": round(time.perf_counter() - t0, 2)}
    rec["hints"] = hints(rec)
    if signaled:
        rec["exit_code"] = proc.wait()
    elif k32.WaitForSingleObject(h, int(linger * 1000)) == 0:
        rec["ended_by_itself_after_s"] = round(time.perf_counter() - t0, 2)
        rec["exit_code"] = proc.wait()
    else:
        rec["threads_after_linger"] = thread_report(proc.pid, h, mods, tpp)
        proc.kill()
        proc.wait()
        rec["terminated_by_probe_after_s"] = round(time.perf_counter() - t0, 2)
    left = max(2.0, linger - (time.perf_counter() - t0)) if signaled else 2.0
    closed = all(e.wait(left) for e in eof.values())
    rec["pipes_closed_after_s" if closed else "pipes_still_open_after_s"] = \
        round(time.perf_counter() - t0, 2)
    return rec


def snapshot(pid, tpp):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE, False, pid)
    if not h:
        print(f"cannot open process {pid} (error {ctypes.get_last_error()}); it may have ended, "
              "or it belongs to another user (try an elevated prompt)", file=sys.stderr)
        raise SystemExit(2)
    mods = modules(h)
    code = W.DWORD()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    rec = {"ok": False, "pid": pid, "process_signaled": k32.WaitForSingleObject(h, 0) == 0,
           "exit_code": None if code.value == STILL_ACTIVE else code.value,
           "threads": thread_report(pid, h, mods, tpp), "children": children(pid),
           "modules": [m[0] for m in mods]}
    rec["hints"] = hints(rec)
    k32.CloseHandle(h)
    return rec


# ---------------------------------------------------------------- output

BANNER_LOGO = (r" \\      //", r"  \\    //", r"   \\  //", r"    \\//", r"     \/")


def enable_ansi():
    if os.environ.get("NO_COLOR"):
        return False
    try:
        k32.GetStdHandle.restype = W.HANDLE
        handle = k32.GetStdHandle(-11)
        mode = W.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004 or k32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def print_banner():
    sep = " · " if (sys.stdout.encoding or "").lower().replace("-", "") == "utf8" else " - "
    text = ["", f"Roan System Labs{sep}win-exit-hang-probe v{__version__}",
            "https://roansystem.com", "", ""]
    colour = enable_ansi()
    cyan, bold, reset = ("\033[36m", "\033[1m", "\033[0m") if colour else ("", "", "")
    print()
    for logo, line in zip(BANNER_LOGO, text):
        line = f"{bold}{line}{reset}" if colour and "Roan" in line else line
        print(f"{cyan}{logo:<14}{reset}{line}".rstrip())
    print()


def print_threads(rows, indent="    "):
    for t in rows:
        if "error" in t:
            print(f"{indent}tid {t['tid']}: {t['error']}")
            continue
        state = "running" if t["running"] else f"ended {t['exit_code']}"
        print(f"{indent}tid {t['tid']:<6} {t['start']:<28} {t['kind']}")
        print(f"{indent}{'':11}{state}, started {t['age_ms_after_process_start']} ms after the "
              f"process, cpu {t['cpu_ms']} ms")


def print_record(rec, label):
    signaled = "yes (the process has ended)" if rec["process_signaled"] else "no (still alive)"
    when = (f", inspected {rec['inspected_after_s']} s after launch"
            if "inspected_after_s" in rec else "")
    print(f"{label}: pid {rec['pid']}{when}")
    print(f"  process signaled: {signaled}")
    if "stdout_eof" in rec:
        print(f"  stdout EOF: {'yes' if rec['stdout_eof'] else 'no'} ({rec['stdout_bytes']} bytes)"
              f"   stderr EOF: {'yes' if rec['stderr_eof'] else 'no'} ({rec['stderr_bytes']} bytes)")
    if not rec["process_signaled"]:
        print(f"  threads still in the process: {len(rec['threads'])}")
        print_threads(rec["threads"])
        print(f"  modules: {', '.join(rec['modules'][:12])}"
              + (f" (+{len(rec['modules']) - 12} more)" if len(rec["modules"]) > 12 else ""))
    elif rec.get("exit_code") is not None:
        print(f"  exit code: {rec['exit_code']} (0x{rec['exit_code'] & 0xffffffff:x})")
    if rec.get("children"):
        print("  child processes still running: "
              + ", ".join(f"{c['name']} (pid {c['pid']})" for c in rec["children"]))
    for h in rec["hints"]:
        print(f"  hint: {h}")
    if "ended_by_itself_after_s" in rec:
        code = rec["exit_code"]
        print(f"  then: ended by itself after {rec['ended_by_itself_after_s']} s, "
              f"exit code {code} (0x{code & 0xffffffff:x})")
    elif "terminated_by_probe_after_s" in rec:
        n = len(rec["threads_after_linger"])
        print(f"  then: still alive with {n} thread(s), terminated by the probe after "
              f"{rec['terminated_by_probe_after_s']} s")
    if "pipes_closed_after_s" in rec and rec["process_signaled"]:
        print(f"  then: stdout/stderr reached EOF after {rec['pipes_closed_after_s']} s")
    elif "pipes_still_open_after_s" in rec:
        print(f"  then: stdout/stderr still open after {rec['pipes_still_open_after_s']} s "
              "(the probe does not close handles held by other processes)")


def main():
    ap = argparse.ArgumentParser(
        prog="exit_hang_probe.py",
        description="Find out what keeps a Windows process from exiting.",
        epilog="Example: python exit_hang_probe.py --runs 200 --timeout 5 -- my_tool.exe --check")
    ap.add_argument("--pid", type=int, help="inspect a process that is already running, then exit")
    ap.add_argument("--runs", type=int, default=20, help="launches (default 20)")
    ap.add_argument("--timeout", type=float, default=5,
                    help="seconds before a launch counts as hung (default 5)")
    ap.add_argument("--parallel", type=int, default=1, help="launches in flight (default 1)")
    ap.add_argument("--priority", choices=["normal", "below"], default="normal",
                    help="priority class of the launched program (default normal)")
    ap.add_argument("--linger", type=float, default=30,
                    help="seconds to keep watching a hung process before terminating it "
                         "(default 30)")
    ap.add_argument("--stdin-hex", default="", help="bytes to write to stdin, as hex (default: "
                    "none, stdin is closed at once)")
    ap.add_argument("--show", type=int, default=3,
                    help="hung launches to print in full (default 3; all are in --json)")
    ap.add_argument("--json", help="write every launch and the summary to this file")
    ap.add_argument("--no-banner", action="store_true", help="skip the start-up header")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("program", nargs=argparse.REMAINDER, help="-- PROGRAM [ARGS...]")
    a = ap.parse_args()
    cmd = a.program[1:] if a.program[:1] == ["--"] else a.program
    if not cmd and a.pid is None:
        ap.error("give a program after -- or --pid PID")
    if a.runs < 1 or a.parallel < 1 or a.timeout <= 0 or a.linger < 0:
        ap.error("--runs and --parallel must be >= 1, --timeout > 0, --linger >= 0")
    if cmd:
        found = os.path.abspath(cmd[0]) if os.path.isfile(cmd[0]) else shutil.which(cmd[0])
        if not found:
            ap.error(f"program not found: {cmd[0]}")
        cmd = [found] + cmd[1:]
    if not (a.no_banner or not sys.stdout.isatty()):
        print_banner()
    tpp = tpp_worker_offsets()

    if a.pid is not None:
        rec = snapshot(a.pid, tpp)
        print_record(rec, "snapshot")
        if a.json:
            with open(a.json, "w", encoding="utf8") as f:
                json.dump({"version": __version__, "snapshot": rec}, f, indent=1)
        return 0

    stdin_bytes = bytes.fromhex(a.stdin_hex) if a.stdin_hex else b""
    with ThreadPoolExecutor(a.parallel) as pool:
        results = list(pool.map(lambda _: launch(cmd, stdin_bytes, a.timeout, a.priority,
                                                 a.linger, tpp), range(a.runs)))
    ok = [r for r in results if r["ok"]]
    hung = [r for r in results if not r["ok"]]
    codes = {}
    for r in ok:
        codes[r["exit_code"]] = codes.get(r["exit_code"], 0) + 1
    summary = {"program": os.path.basename(cmd[0]), "runs": a.runs, "parallel": a.parallel,
               "priority": a.priority, "timeout_s": a.timeout, "finished": len(ok),
               "hung": len(hung),
               "exit_codes": {f"0x{c & 0xffffffff:x}": n for c, n in sorted(codes.items())},
               "slowest_ms": max((r["ms"] for r in ok), default=None),
               "tpp_worker_ntdll_offsets": [hex(o) for o in sorted(tpp)]}
    print(f"{summary['program']}: {a.runs} runs ({a.parallel} in flight, {a.priority} priority), "
          f"{len(ok)} finished within {a.timeout:g} s, {len(hung)} hung")
    if ok:
        print("  exit codes: " + ", ".join(f"{c} x{n}" for c, n in summary["exit_codes"].items())
              + f"; slowest {summary['slowest_ms']} ms")
    for i, r in enumerate(hung[:a.show], 1):
        print()
        print_record(r, f"hung launch {i}/{len(hung)}")
    if len(hung) > a.show:
        more = len(hung) - a.show
        print(f"\n({more} more hung launch{'es' if more > 1 else ''} not shown; "
              "use --show or --json)")
    if a.json:
        with open(a.json, "w", encoding="utf8") as f:
            json.dump({"version": __version__, "summary": summary, "launches": results}, f,
                      indent=1)
    return 1 if hung else 0


if __name__ == "__main__":
    sys.exit(main())
