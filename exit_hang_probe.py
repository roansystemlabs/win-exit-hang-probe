"""win-exit-hang-probe: why doesn't a Windows process exit?

Launches a program many times (or looks at one that is already running) and, when it does not
finish in time, records what is still keeping it alive before anything is killed:

  - whether the process object is signaled (has the process really ended?)
  - whether its stdout/stderr pipes reached end-of-file (does someone else still hold them?)
  - every thread still in the process: start address as module+offset, whether it is the
    initial thread or a default thread-pool worker (ntdll TppWorkerThread), CPU time, age
  - the processes it started that are still running, and the loaded modules

Then it keeps watching for --linger seconds to see whether everything ends by itself. Each
launch runs in its own Windows job object; whatever is still running in that job at the end
(the program, its children and grandchildren) is terminated, so nothing is left behind.
Standard library only, Windows, Python 3.9+.

usage:
  python exit_hang_probe.py [options] -- PROGRAM [ARGS...]
  python exit_hang_probe.py --pid PID
"""
import argparse
import ctypes
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from ctypes import wintypes as W

__version__ = "1.1.1"

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


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", W.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD),
                ("SchedulingClass", W.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [("NumberOfAssignedProcesses", W.DWORD), ("NumberOfProcessIdsInList", W.DWORD),
                ("ProcessIdList", ctypes.c_size_t * 1024)]


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
k32.WaitForSingleObject.restype = W.DWORD
k32.GetThreadTimes.argtypes = [W.HANDLE, U64P, U64P, U64P, U64P]
k32.GetProcessTimes.argtypes = [W.HANDLE, U64P, U64P, U64P, U64P]
k32.GetExitCodeThread.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
k32.ResumeThread.argtypes = [W.HANDLE]
k32.ResumeThread.restype = W.DWORD
k32.CloseHandle.argtypes = [W.HANDLE]
k32.CreateJobObjectW.restype = W.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
k32.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
k32.QueryInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD,
                                          ctypes.c_void_p]
k32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
k32.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
ntdll.NtQueryInformationThread.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.ULONG,
                                           ctypes.c_void_p]
psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.POINTER(W.HMODULE), W.DWORD,
                                       ctypes.POINTER(W.DWORD), W.DWORD]
psapi.GetModuleBaseNameW.argtypes = [W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD]
psapi.GetModuleInformation.argtypes = [W.HANDLE, W.HMODULE, ctypes.POINTER(MODULEINFO), W.DWORD]

THREAD_SUSPEND_RESUME, THREAD_QUERY_INFORMATION, SYNCHRONIZE = 0x2, 0x40, 0x100000
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x400, 0x10
PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x1, 0x100
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPTHREAD, LIST_MODULES_ALL, THREAD_QUERY_WIN32_START_ADDRESS = 0x4, 0x3, 9
CREATE_SUSPENDED, BELOW_NORMAL_PRIORITY_CLASS, STILL_ACTIVE = 0x4, 0x4000, 0x103
JOB_PROCESS_ID_LIST, JOB_EXTENDED_LIMITS, JOB_KILL_ON_CLOSE = 3, 9, 0x2000
WAIT_OBJECT_0 = 0
MAX_SECONDS = 86400  # --timeout and --linger: Windows waits take milliseconds in 32 bits
CHECKPOINT_S = 2  # --json is rewritten at most this often while launches finish
CRT_MODULES = ("ucrtbase", "msvcrt", "msvcr", "vcruntime", "msvcp")
IGNORED_CHILDREN = {"conhost.exe"}  # the console host every console program gets


class ProbeError(Exception):
    """A problem with the command line or the environment, not a hang: exit code 2."""


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


def processes():
    """{pid: (parent_pid, name)} of every live process."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry, out = PROCESSENTRY32W(), {}
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def children(pid):
    """Live processes whose parent process ID is pid: [{"pid", "name"}]. Parent IDs are not
    reference-counted by Windows, so treat a match as a lead, not proof."""
    return [{"pid": p, "name": name} for p, (parent, name) in processes().items()
            if parent == pid and p != pid and name.lower() not in IGNORED_CHILDREN]


def named(pids):
    """[{"pid", "name"}] for the pids that are still live processes, without the console host.
    A job can still list a process for a moment after it has exited."""
    table = processes()
    return [{"pid": p, "name": table[p][1]} for p in pids
            if p in table and table[p][1].lower() not in IGNORED_CHILDREN]


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


_TPP_CALLBACK_TYPE = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_tpp_seen, _tpp_done = [], threading.Event()


@_TPP_CALLBACK_TYPE
def _tpp_callback(instance, context):
    # Module level, so it stays alive even if the pool runs it late.
    _tpp_seen.append(k32.GetCurrentThreadId())
    _tpp_done.set()


def tpp_worker_offsets():
    """ntdll offsets where default thread-pool workers start (TppWorkerThread), learnt by
    queueing a callback in this process and reading the start address of the thread that
    runs it. ntdll is mapped at the same base in every process of a boot session."""
    if not k32.TrySubmitThreadpoolCallback(_tpp_callback, None, None) or not _tpp_done.wait(5):
        return set()
    ntdll_mod = [m for m in modules(k32.GetCurrentProcess()) if m[0].lower() == "ntdll.dll"]
    out = set()
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, _tpp_seen[0])
    if h and ntdll_mod:
        _, base, size, _ = ntdll_mod[0]
        addr = start_address(h)
        if base <= addr < base + size:
            out.add(addr - base)
    if h:
        k32.CloseHandle(h)
    return out


def describe(addr, mods):
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
    ntdll_mod = next((m for m in mods if m[0].lower() == "ntdll.dll"), None)
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
        elif ntdll_mod and addr - ntdll_mod[1] in tpp:
            kind = "thread-pool worker (ntdll TppWorkerThread)"
        elif mods and mods[0][1] <= addr < mods[0][1] + mods[0][2]:
            kind = "thread started by the executable"
        elif addr:
            kind = "thread started by " + describe(addr, mods).split("+")[0]
        else:
            kind = "unknown start"
        rows.append({"tid": tid, "start": describe(addr, mods), "kind": kind,
                     "running": k32.WaitForSingleObject(h, 0) != WAIT_OBJECT_0,
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
            if who:
                tail = f"; processes it started are still running: {who}"
            elif rec.get("children_from") == "job":
                tail = ("; no process it started is still running, so a process outside it must"
                        " have duplicated the handle")
            else:
                tail = ("; no child process of it is still running, so look for a grandchild or"
                        " a process that duplicated the handle")
            out.append("the process has ended but its stdout/stderr pipe is still open: another "
                       "process inherited the pipe and still holds it" + tail)
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


# ---------------------------------------------------------------- jobs and stopping

STOP = threading.Event()  # set by Ctrl+C or by a launch that cannot start
# Re-entrant: a Ctrl+C handler can run on the main thread while it already holds the lock
# (inside stop_all), and must not wait for itself.
_live_jobs, _jobs_lock = set(), threading.RLock()


class Job:
    """A job object that ends every process in it when its last handle is closed."""

    def __init__(self):
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ProbeError(f"cannot create a job object (error {ctypes.get_last_error()})")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_CLOSE
        if not k32.SetInformationJobObject(self.handle, JOB_EXTENDED_LIMITS, ctypes.byref(info),
                                           ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k32.CloseHandle(self.handle)
            raise ProbeError(f"cannot configure a job object (error {err})")

    def pids(self):
        ids = JOBOBJECT_BASIC_PROCESS_ID_LIST()
        k32.QueryInformationJobObject(self.handle, JOB_PROCESS_ID_LIST, ctypes.byref(ids),
                                      ctypes.sizeof(ids), None)
        return [ids.ProcessIdList[i] for i in range(ids.NumberOfProcessIdsInList)]

    def terminate(self):
        k32.TerminateJobObject(self.handle, 1)

    def close(self):
        k32.CloseHandle(self.handle)  # kill-on-close: anything still in the job ends here


def register(job):
    """Track a job so that Ctrl+C can end it. False if the run is already stopping."""
    with _jobs_lock:
        if STOP.is_set():
            return False
        _live_jobs.add(job)
        return True


def unregister(job):
    with _jobs_lock:
        _live_jobs.discard(job)


def stop_all():
    """Stop new launches and end every launched process tree now."""
    STOP.set()
    with _jobs_lock:
        for job in _live_jobs:
            job.terminate()


def start_in_job(cmd, priority, job):
    """Start cmd suspended, put it in job, then let it run: nothing it starts can escape."""
    flags = CREATE_SUSPENDED | (BELOW_NORMAL_PRIORITY_CLASS if priority == "below" else 0)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=flags)
    except OSError as e:
        code = f" (WinError {e.winerror})" if getattr(e, "winerror", None) else ""
        raise ProbeError(f"cannot start {cmd[0]}: {e.strerror or e}{code}") from None
    access = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE | PROCESS_TERMINATE
              | PROCESS_SET_QUOTA)
    h = k32.OpenProcess(access, False, proc.pid)
    problem = None
    if not h:
        problem = f"cannot open the launched process (error {ctypes.get_last_error()})"
    elif not k32.AssignProcessToJobObject(job.handle, h):
        problem = (f"cannot put the launched process in a job object "
                   f"(error {ctypes.get_last_error()})")
    else:
        resumed = 0
        for tid in thread_ids(proc.pid):
            th = k32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
            if th:
                resumed += k32.ResumeThread(th) != 0xFFFFFFFF
                k32.CloseHandle(th)
        if not resumed:
            problem = "cannot resume the launched process"
    if problem:
        proc.kill()
        proc.wait()
        if h:
            k32.CloseHandle(h)
        raise ProbeError(problem)
    return proc, h


# ---------------------------------------------------------------- launching

def launch(cmd, stdin_bytes, timeout, priority, linger, tpp, progress):
    """One launch. Returns its record, or None if the run was stopped before it could tell."""
    job = Job()
    if not register(job):
        job.close()
        return None
    h = None
    try:
        proc, h = start_in_job(cmd, priority, job)
        if STOP.is_set():
            job.terminate()
        return watch(proc, h, job, stdin_bytes, timeout, linger, tpp, progress)
    finally:
        unregister(job)
        job.close()
        if h:
            k32.CloseHandle(h)


def watch(proc, h, job, stdin_bytes, timeout, linger, tpp, progress):
    t0 = time.perf_counter()
    got = {"stdout": bytearray(), "stderr": bytearray()}
    eof = {"stdout": threading.Event(), "stderr": threading.Event()}

    def pump(name, stream):
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            got[name] += chunk
        eof[name].set()

    def since_start():
        return round(time.perf_counter() - t0, 2)

    def pipes_closed(seconds):
        end = time.perf_counter() + seconds
        return all(e.wait(max(0.0, end - time.perf_counter())) for e in eof.values())

    for n in got:
        threading.Thread(target=pump, args=(n, getattr(proc, n)), daemon=True).start()
    try:
        if stdin_bytes:
            proc.stdin.write(stdin_bytes)
        proc.stdin.close()
    except OSError:
        pass
    finished = k32.WaitForSingleObject(h, int(timeout * 1000)) == WAIT_OBJECT_0
    finished = finished and pipes_closed(timeout - (time.perf_counter() - t0))
    if STOP.is_set():
        proc.wait()
        return None
    if finished:
        rec = {"ok": True, "exit_code": proc.wait(),
               "ms": round((time.perf_counter() - t0) * 1000, 1)}
        left = named(job.pids())
        if left:
            rec["left_running"] = left
            job.terminate()
        return rec

    progress.lingering(+1)
    try:
        mods = modules(h)
        signaled = k32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0
        rec = {"ok": False, "pid": proc.pid, "timeout_s": timeout, "process_signaled": signaled,
               "threads": thread_report(proc.pid, h, mods, tpp),
               "children": named(p for p in job.pids() if p != proc.pid),
               "children_from": "job",
               "stdout_eof": eof["stdout"].is_set(), "stderr_eof": eof["stderr"].is_set(),
               "stdout_bytes": len(got["stdout"]), "stderr_bytes": len(got["stderr"]),
               "modules": [m[0] for m in mods],
               "inspected_after_s": since_start()}
        rec["hints"] = hints(rec)
        deadline = time.perf_counter() + linger
        alive = not signaled and k32.WaitForSingleObject(h, int(linger * 1000)) != WAIT_OBJECT_0
        if STOP.is_set():  # Ctrl+C: the job, and so everything in it, is already terminated
            rec["exit_code"] = proc.wait()
            rec["interrupted_after_s"] = since_start()
            return rec
        if not signaled and not alive:
            rec["ended_by_itself_after_s"] = since_start()
        if not alive and pipes_closed(deadline - time.perf_counter()):
            rec["pipes_closed_after_s"] = since_start()
        still = named(job.pids())
        if alive:
            rec["threads_after_linger"] = thread_report(proc.pid, h, mods, tpp)
        if still or alive:
            job.terminate()
            rec["terminated_by_probe_after_s"] = since_start()
            rec["terminated"] = still
        rec["exit_code"] = proc.wait()
        if "pipes_closed_after_s" not in rec:
            closed = pipes_closed(2.0)
            rec["pipes_closed_after_s" if closed else "pipes_still_open_after_s"] = since_start()
        return rec
    finally:
        progress.lingering(-1)


def snapshot(pid, tpp):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE, False, pid)
    if not h:
        raise ProbeError(f"cannot open process {pid} (error {ctypes.get_last_error()}); it may "
                         "have ended, or it belongs to another user (try an elevated prompt)")
    mods = modules(h)
    code = W.DWORD()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    rec = {"ok": False, "pid": pid, "process_signaled": k32.WaitForSingleObject(h, 0) == 0,
           "exit_code": None if code.value == STILL_ACTIVE else code.value,
           "threads": thread_report(pid, h, mods, tpp), "children": children(pid),
           "children_from": "parent pid", "modules": [m[0] for m in mods]}
    rec["hints"] = hints(rec)
    k32.CloseHandle(h)
    return rec


class Progress:
    """One status line on stderr, only when stderr is a terminal."""

    def __init__(self, runs, linger):
        self.runs, self.linger, self.done, self.hung, self.waiting = runs, linger, 0, 0, 0
        self.lock = threading.Lock()
        self.enabled = sys.stderr.isatty()
        self.width = 0

    def lingering(self, delta):
        with self.lock:
            self.waiting += delta

    def finished(self, rec):
        with self.lock:
            self.done += 1
            self.hung += rec is not None and not rec["ok"]

    def show(self):
        if not self.enabled:
            return
        with self.lock:
            line = f"{self.done}/{self.runs} launches done, {self.hung} hung"
            if self.waiting:
                line += f"; inspecting or watching {self.waiting} (up to {self.linger:g} s)"
        sys.stderr.write("\r" + line.ljust(self.width))
        sys.stderr.flush()
        self.width = len(line)

    def clear(self):
        if self.enabled and self.width:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()


def run_launches(cmd, a, stdin_bytes, tpp, checkpoint=None):
    """All launches. Returns (records, interrupted); records keep launch order.

    checkpoint(records), if given, is called from this thread with the launches done so far,
    at most every CHECKPOINT_S seconds."""
    results = [None] * a.runs
    progress = Progress(a.runs, a.linger)

    def one(i):
        if not STOP.is_set():
            results[i] = launch(cmd, stdin_bytes, a.timeout, a.priority, a.linger, tpp,
                                progress)
            progress.finished(results[i])

    def on_ctrl_c(signum, frame):
        if STOP.is_set():
            # Second Ctrl+C: leave now without touching the jobs (the first one may still be
            # terminating them). Exiting closes every job handle, and kill-on-close ends
            # every launched process tree.
            sys.stderr.write("\nsecond Ctrl+C: leaving now; every launched process is terminated\n")
            sys.stderr.flush()
            os._exit(130)
        stop_all()

    previous = signal.signal(signal.SIGINT, on_ctrl_c)
    pool = ThreadPoolExecutor(a.parallel)
    error, in_flight, next_run = None, set(), 0
    saved_at, unsaved = time.monotonic(), False
    try:
        while True:
            # Submit only what can run now, so a stop leaves nothing queued.
            while not STOP.is_set() and next_run < a.runs and len(in_flight) < a.parallel:
                in_flight.add(pool.submit(one, next_run))
                next_run += 1
            if not in_flight:
                break
            done, in_flight = wait(in_flight, timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                if f.exception() is not None and error is None:
                    error = f.exception()
                    stop_all()
            unsaved = unsaved or bool(done)
            if checkpoint and unsaved and time.monotonic() - saved_at >= CHECKPOINT_S:
                checkpoint([r for r in results if r is not None])
                saved_at, unsaved = time.monotonic(), False
            progress.show()
    finally:
        pool.shutdown(wait=True)
        progress.clear()
        signal.signal(signal.SIGINT, previous)
    if error is not None:
        raise error
    return [r for r in results if r is not None], STOP.is_set()


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


def plist(rows):
    return ", ".join(f"{c['name']} (pid {c['pid']})" for c in rows)


def print_record(rec, label):
    signaled = "yes (the process has ended)" if rec["process_signaled"] else "no (still alive)"
    when = (f", inspected {rec['inspected_after_s']} s after launch"
            if "inspected_after_s" in rec else "")
    print(f"{label}: pid {rec['pid']}{when}")
    print(f"  process signaled: {signaled}")
    if "stdout_eof" in rec:
        out_eof, err_eof = ("yes" if rec[k] else "no" for k in ("stdout_eof", "stderr_eof"))
        print(f"  stdout EOF: {out_eof} ({rec['stdout_bytes']} bytes)"
              f"   stderr EOF: {err_eof} ({rec['stderr_bytes']} bytes)")
    if not rec["process_signaled"]:
        print(f"  threads still in the process: {len(rec['threads'])}")
        print_threads(rec["threads"])
        print(f"  modules: {', '.join(rec['modules'][:12])}"
              + (f" (+{len(rec['modules']) - 12} more)" if len(rec["modules"]) > 12 else ""))
    elif rec.get("exit_code") is not None:
        print(f"  exit code: {rec['exit_code']} (0x{rec['exit_code'] & 0xffffffff:x})")
    if rec.get("children"):
        print("  child processes still running: " + plist(rec["children"]))
    for h in rec["hints"]:
        print(f"  hint: {h}")
    if "ended_by_itself_after_s" in rec:
        code = rec["exit_code"]
        print(f"  then: ended by itself after {rec['ended_by_itself_after_s']} s, "
              f"exit code {code} (0x{code & 0xffffffff:x})")
    elif "threads_after_linger" in rec:
        n = len(rec["threads_after_linger"])
        print(f"  then: still alive with {n} thread(s), terminated by the probe after "
              f"{rec['terminated_by_probe_after_s']} s")
    if "pipes_closed_after_s" in rec and rec["process_signaled"]:
        print(f"  then: stdout/stderr reached EOF after {rec['pipes_closed_after_s']} s")
    others = [c for c in rec.get("terminated", []) if c["pid"] != rec["pid"]]
    if others:
        print(f"  then: still running after --linger and terminated by the probe: {plist(others)}")
    if "pipes_still_open_after_s" in rec:
        print(f"  then: stdout/stderr still open after {rec['pipes_still_open_after_s']} s "
              "(held by a process that is not in the launch's job; the probe does not close "
              "handles in other processes)")
    if "interrupted_after_s" in rec:
        print(f"  then: interrupted by Ctrl+C after {rec['interrupted_after_s']} s; the probe "
              "terminated it and everything it started")


def parse_args():
    ap = argparse.ArgumentParser(
        prog="exit_hang_probe.py",
        description="Find out what keeps a Windows process from exiting.",
        epilog="Example: python exit_hang_probe.py --runs 200 --parallel 8 -- my_tool.exe --check")
    ap.add_argument("--pid", type=int, help="inspect a process that is already running, then exit")
    ap.add_argument("--runs", type=int, default=20, help="launches (default 20)")
    ap.add_argument("--timeout", type=float, default=5,
                    help="seconds before a launch counts as hung (default 5)")
    ap.add_argument("--parallel", type=int, default=1, help="launches in flight (default 1)")
    ap.add_argument("--priority", choices=["normal", "below"], default="normal",
                    help="priority class of the launched program (default normal)")
    ap.add_argument("--linger", type=float, default=30,
                    help="seconds to keep watching a hung launch before terminating it and "
                         "everything it started (default 30)")
    ap.add_argument("--stdin-hex", default="", help="bytes to write to stdin, as hex (default: "
                    "none, stdin is closed at once)")
    ap.add_argument("--show", type=int, default=3,
                    help="hung launches to print in full (default 3; all are in --json)")
    ap.add_argument("--json", help="write every launch and the summary to this file")
    ap.add_argument("--no-banner", action="store_true", help="skip the start-up header")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("program", nargs=argparse.REMAINDER, help="-- PROGRAM [ARGS...]")
    a = ap.parse_args()
    a.cmd = a.program[1:] if a.program[:1] == ["--"] else a.program
    if a.pid is not None and a.cmd:
        ap.error("give either --pid PID or a program after --, not both")
    if not a.cmd and a.pid is None:
        ap.error("give a program after -- or --pid PID")
    if not 1 <= a.runs <= 1_000_000 or not 1 <= a.parallel <= 256 or a.show < 0:
        ap.error("--runs must be 1..1000000, --parallel 1..256, --show >= 0")
    if not (math.isfinite(a.timeout) and 0 < a.timeout <= MAX_SECONDS):
        ap.error(f"--timeout must be more than 0 and at most {MAX_SECONDS} seconds")
    if not (math.isfinite(a.linger) and 0 <= a.linger <= MAX_SECONDS):
        ap.error(f"--linger must be 0 to {MAX_SECONDS} seconds")
    try:
        a.stdin_bytes = bytes.fromhex(a.stdin_hex)
    except ValueError:
        ap.error(f"--stdin-hex is not hex: {a.stdin_hex!r} (example: --stdin-hex 0d0a)")
    if a.cmd:
        found = os.path.abspath(a.cmd[0]) if os.path.isfile(a.cmd[0]) else shutil.which(a.cmd[0])
        if not found:
            ap.error(f"program not found: {a.cmd[0]}")
        a.cmd = [found] + a.cmd[1:]
    if a.json:
        # Checked last, before anything is launched: the file is created now ("partial": true)
        # so that a path that cannot be written is a usage error, not a lost run.
        a.json = os.path.abspath(a.json)
        if os.path.isdir(a.json):
            ap.error(f"--json must name a file, not a folder: {a.json}")
        try:
            write_json(a.json, {"version": __version__, "partial": True, "launches": []})
        except OSError as e:
            ap.error(f"cannot write the --json file {a.json}: {e.strerror or e}")
    return a


def write_json(path, data):
    """Replace path in one step: a reader sees the previous content or the new one, never half."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def summarize(a, results, interrupted):
    ok = [r for r in results if r["ok"]]
    codes = {}
    for r in ok:
        codes[r["exit_code"]] = codes.get(r["exit_code"], 0) + 1
    return {"program": os.path.basename(a.cmd[0]), "runs": a.runs,
            "completed": len(results), "interrupted": interrupted, "parallel": a.parallel,
            "priority": a.priority, "timeout_s": a.timeout, "linger_s": a.linger,
            "finished": len(ok), "hung": len(results) - len(ok),
            "exit_codes": {f"0x{c & 0xffffffff:x}": n for c, n in sorted(codes.items())},
            "slowest_ms": max((r["ms"] for r in ok), default=None),
            "finished_but_left_processes": sum(1 for r in ok if r.get("left_running")),
            "tpp_worker_ntdll_offsets": [hex(o) for o in sorted(a.tpp)]}


def main():
    a = parse_args()
    if not (a.no_banner or not sys.stdout.isatty()):
        print_banner()
    tpp = a.tpp = tpp_worker_offsets()
    warned = []

    def checkpoint(done):
        try:
            write_json(a.json, {"version": __version__, "partial": True,
                                "summary": summarize(a, done, False), "launches": done})
        except OSError as e:
            if not warned:  # keep going: the run matters more; the final write tries again
                warned.append(e)
                print(f"exit_hang_probe.py: warning: cannot update {a.json}: {e.strerror or e}",
                      file=sys.stderr)

    try:
        if a.pid is not None:
            rec = snapshot(a.pid, tpp)
            print_record(rec, "snapshot")
            if a.json:
                return final_json(a.json, {"version": __version__, "partial": False,
                                           "snapshot": rec}, 0)
            return 0
        results, interrupted = run_launches(a.cmd, a, a.stdin_bytes, tpp,
                                            checkpoint if a.json else None)
    except ProbeError as e:
        print(f"exit_hang_probe.py: error: {e}", file=sys.stderr)
        return 2
    ok = [r for r in results if r["ok"]]
    hung = [r for r in results if not r["ok"]]
    left = [r for r in ok if r.get("left_running")]
    left_names = {}
    for r in left:
        for c in r["left_running"]:
            left_names[c["name"]] = left_names.get(c["name"], 0) + 1
    summary = summarize(a, results, interrupted)
    runs = f"{a.runs} run{'s' if a.runs != 1 else ''}"
    runs = f"{len(results)} of {runs}" if interrupted else runs
    print(f"{summary['program']}: {runs} ({a.parallel} in flight, {a.priority} priority), "
          f"{len(ok)} finished within {a.timeout:g} s, {len(hung)} hung")
    if ok:
        print("  exit codes: " + ", ".join(f"{c} x{n}" for c, n in summary["exit_codes"].items())
              + f"; slowest {summary['slowest_ms']} ms")
    if left:
        print(f"  {len(left)} finished launch(es) left processes running, terminated by the probe: "
              + ", ".join(f"{name} x{n}" for name, n in sorted(left_names.items())))
    for i, r in enumerate(hung[:a.show], 1):
        print()
        print_record(r, f"hung launch {i}/{len(hung)}")
    if len(hung) > a.show:
        more = len(hung) - a.show
        print(f"\n({more} more hung launch{'es' if more > 1 else ''} not shown; "
              "use --show or --json)")
    if interrupted:
        print(f"interrupted by Ctrl+C after {len(results)} of {a.runs} launches; every launched "
              "process was terminated", file=sys.stderr)
    code = 130 if interrupted else 1 if hung else 0
    if a.json:
        return final_json(a.json, {"version": __version__, "partial": False, "summary": summary,
                                   "launches": results}, code)
    return code


def final_json(path, data, code):
    """Write the finished --json file; if that fails, say so and exit with 2, not the result."""
    try:
        write_json(path, data)
    except OSError as e:
        print(f"exit_hang_probe.py: error: cannot write {path}: {e.strerror or e}",
              file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
