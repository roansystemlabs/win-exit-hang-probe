<p align="center">
  <a href="https://roansystem.com">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/roan-system-logo-dark.svg">
      <img src="assets/roan-system-logo-light.svg" alt="Roan System" width="264">
    </picture>
  </a>
</p>

# win-exit-hang-probe

Made by Roan System — https://roansystem.com · GitHub: [@roansystemlabs](https://github.com/roansystemlabs)

**Why doesn't this Windows process exit?** A small probe that launches a program many times (or
inspects one that is already running, by PID) and, when it does not finish in time, records what
is still keeping it alive, *before* anything is killed:

- is the process object signaled, i.e. has the process really ended?
- did its stdout/stderr pipes reach end-of-file, or does another process still hold them?
- which threads are still in it: start address as `module+offset`, whether each one is the
  initial thread, a default thread-pool worker (`ntdll!TppWorkerThread`), a C runtime thread or
  a thread of the program's own code, plus CPU time and age;
- which processes it started are still running, and which modules are loaded.

Then it keeps watching for `--linger` seconds to see whether everything ends by itself. Each
launch runs in its own Windows job object, so at the end nothing is left behind: whatever is still
running (the program, its children, their children) is terminated. One Python file, standard
library only. A tiny C program with deterministic hang-at-exit cases is included as a control.

## When to use it

- A test or CI step times out "sometimes" because a child process finishes its work but never
  exits, or its output pipe never closes.
- A tool exits fine on your machine and hangs on someone else's: run it a few hundred times
  there with `--runs` and `--parallel`, and the first hang comes with the thread list.
- A process is stuck right now: `--pid` gives the same report without launching anything.

## Requirements

- Windows 10 or 11, Python 3.9 or later (standard library only). Tested so far on Windows 11 x64
  with Python 3.12.
- Only for the optional control program: MSVC (Visual Studio or Build Tools with the C++
  workload).

## Usage

```bat
python exit_hang_probe.py --runs 200 --parallel 8 --timeout 5 -- my_tool.exe --check
python exit_hang_probe.py --pid 12345
```

| Option | Meaning |
| --- | --- |
| `-- PROGRAM [ARGS...]` | the program to launch: a path, or a name looked up like `cmd.exe` does (current directory first, then `PATH`) |
| `--pid PID` | inspect a process that is already running, then exit (instead of a program) |
| `--runs N` | launches (default 20, at most 1,000,000) |
| `--parallel K` | launches in flight at the same time (default 1, at most 256); load often makes rare hangs show up |
| `--timeout S` | seconds before a launch counts as hung (default 5, at most 86400) |
| `--linger S` | seconds to keep watching a hung launch before terminating it and everything it started (default 30, at most 86400) |
| `--priority normal\|below` | priority class of the launched program |
| `--stdin-hex HEX` | bytes written to stdin, e.g. `0d0a` (default: none, stdin is closed at once) |
| `--show N` | hung launches printed in full (default 3) |
| `--json FILE` | every launch, the summary and all thread data as JSON; created before the first launch and updated as launches finish (`"partial": true` until the run ends) |
| `--no-banner` | skip the start-up header (it is also skipped when the output is redirected) |
| `--version`, `-h` | print the version, or the help |

A launch counts as finished only when the process has ended **and** both pipes reached EOF
within the timeout. Exit codes: `0` no hang, `1` at least one launch hung, `2` usage error
(including a `--json` file that cannot be written, before or after the run), a program that
cannot be started, or a process that cannot be opened, `130` stopped with Ctrl+C.

The `--json` file is checked before anything is launched, then replaced in one step at most
every 2 s while launches finish, so a run that is killed keeps the launches already done. A
program reading the file never sees it half written, but may briefly find it missing or locked; retry.
If a reader keeps the file open so that it cannot be replaced, an update during the run is
skipped with a warning on stderr (the result and the exit code do not change), and the final
file is written in place.

**How long it takes.** A launch that finishes costs its own run time. A hung launch costs
`--timeout`, then up to `--linger` more: with the defaults, about 35 s. So 20 launches of a
program that always hangs take about 12 minutes one after the other; use `--parallel` and a
shorter `--linger` to go faster. While it runs in a terminal, one status line on stderr shows how
many launches are done, how many hung, and how many are being watched.

**Ctrl+C** stops new launches at once and terminates every launch in flight, with everything it
started; the launches already done are reported. A second Ctrl+C leaves immediately; the job
objects still take everything with them.

## Example output

All output below is real, from Windows 11 (build 26200) and Python 3.12, produced by the version
in this repository. The banner is not shown: it is skipped when the output is redirected.

A program whose entry point returns while a thread-pool worker is still alive (control mode 1,
see below). The initial thread is gone and one `TppWorkerThread` keeps the process open:

```text
> python exit_hang_probe.py --runs 3 --timeout 2 --linger 8 -- build\exit_hang_repro.exe 1
exit_hang_repro.exe: 3 runs (1 in flight, normal priority), 0 finished within 2 s, 3 hung

hung launch 1/3: pid 55804, inspected 2.12 s after launch
  process signaled: no (still alive)
  stdout EOF: no (0 bytes)   stderr EOF: no (0 bytes)
  threads still in the process: 1
    tid 60872  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 106.7 ms after the process, cpu 0.0 ms
  modules: exit_hang_repro.exe, ntdll.dll, KERNEL32.DLL, KERNELBASE.dll
  hint: the initial thread is gone: the entry point returned or called ExitThread, and the process lives on through its remaining threads
  hint: only thread-pool workers are left (1); an entry point that returns instead of calling ExitProcess waits for them. Who queued the work is not visible here
  then: still alive with 1 thread(s), terminated by the probe after 10.24 s
...
```

(Excerpt: hung launches 2/3 and 3/3 print the same report with other PIDs and are left out.)

A program that has already exited, but a child it started inherited its stdout/stderr, so the
caller's read never reaches EOF (control mode 4). The child ends by itself after 5 s:

```text
> python exit_hang_probe.py --runs 2 --timeout 2 --linger 8 -- build\exit_hang_repro.exe 4
exit_hang_repro.exe: 2 runs (1 in flight, normal priority), 0 finished within 2 s, 2 hung

hung launch 1/2: pid 56476, inspected 2.14 s after launch
  process signaled: yes (the process has ended)
  stdout EOF: no (0 bytes)   stderr EOF: no (0 bytes)
  exit code: 1549 (0x60d)
  child processes still running: exit_hang_repro.exe (pid 58124)
  hint: the process has ended but its stdout/stderr pipe is still open: another process inherited the pipe and still holds it; processes it started are still running: exit_hang_repro.exe (pid 58124)
  then: stdout/stderr reached EOF after 5.02 s
...
```

(Excerpt: hung launch 2/2 is the same and is left out.)

An ordinary Python script that prints its result and then starts a non-daemon thread it never
joins. The interpreter's main thread waits for it at shutdown:

```python
# forgot_join.py
import threading
import time

print("work done")
threading.Thread(target=time.sleep, args=(8,)).start()  # non-daemon, never joined
```

```text
> python exit_hang_probe.py --runs 3 --timeout 2 --show 1 -- python forgot_join.py
python.EXE: 3 runs (1 in flight, normal priority), 0 finished within 2 s, 3 hung

hung launch 1/3: pid 52840, inspected 2.11 s after launch
  process signaled: no (still alive)
  stdout EOF: no (11 bytes)   stderr EOF: no (0 bytes)
  threads still in the process: 5
    tid 57300  python.EXE+0x12a0            initial thread (executable entry point)
               running, started 0.0 ms after the process, cpu 15.6 ms
    tid 53792  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 84.6 ms after the process, cpu 0.0 ms
    tid 51476  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 84.7 ms after the process, cpu 0.0 ms
    tid 19648  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 84.8 ms after the process, cpu 0.0 ms
    tid 57676  ucrtbase.dll+0x2cd00         thread started by ucrtbase.dll
               running, started 102.9 ms after the process, cpu 0.0 ms
  modules: python.EXE, ntdll.dll, KERNEL32.DLL, KERNELBASE.dll, ucrtbase.dll, python312.dll, VCRUNTIME140.dll, WS2_32.dll, RPCRT4.dll, VERSION.dll, ADVAPI32.dll, msvcrt.dll (+4 more)
  hint: the initial thread is still running: main() has not returned, or the runtime is waiting at shutdown (for example joining a thread that never ends)
  hint: some threads were started by ucrtbase.dll, the C runtime's thread start (_beginthreadex, std::thread, language runtimes): the program created them, and the real thread function is not visible from the start address
  then: ended by itself after 8.02 s, exit code 0 (0x0)

(2 more hung launches not shown; use --show or --json)
```

A script that hangs and has started a child sharing its output. After `--linger`, the probe
terminates both, so nothing is left running:

```python
# parent_and_child.py
import subprocess
import sys
import time

if sys.argv[1:] == ["child"]:
    time.sleep(600)
else:
    subprocess.Popen([sys.executable, __file__, "child"])  # inherits stdout/stderr
    time.sleep(600)
```

```text
> python exit_hang_probe.py --runs 1 --timeout 2 --linger 3 -- python parent_and_child.py
python.EXE: 1 run (1 in flight, normal priority), 0 finished within 2 s, 1 hung

hung launch 1/1: pid 40120, inspected 2.1 s after launch
  process signaled: no (still alive)
  stdout EOF: no (0 bytes)   stderr EOF: no (0 bytes)
  threads still in the process: 4
    tid 61296  python.EXE+0x12a0            initial thread (executable entry point)
               running, started 0.0 ms after the process, cpu 15.6 ms
    tid 52708  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 109.5 ms after the process, cpu 0.0 ms
    tid 47584  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 109.5 ms after the process, cpu 0.0 ms
    tid 32936  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 109.6 ms after the process, cpu 0.0 ms
  modules: python.EXE, ntdll.dll, KERNEL32.DLL, KERNELBASE.dll, ucrtbase.dll, python312.dll, VCRUNTIME140.dll, WS2_32.dll, RPCRT4.dll, VERSION.dll, ADVAPI32.dll, msvcrt.dll (+3 more)
  child processes still running: python.exe (pid 58768)
  hint: the initial thread is still running: main() has not returned, or the runtime is waiting at shutdown (for example joining a thread that never ends)
  then: still alive with 4 thread(s), terminated by the probe after 5.21 s
  then: still running after --linger and terminated by the probe: python.exe (pid 58768)
```

A healthy program gives one summary line:

```text
> python exit_hang_probe.py --runs 20 -- cmd /c exit 3
cmd.EXE: 20 runs (1 in flight, normal priority), 20 finished within 5 s, 0 hung
  exit codes: 0x3 x20; slowest 7.8 ms
```

The `ntdll.dll+0x...` offset of `TppWorkerThread` differs between Windows builds; the probe
learns it at start-up by queueing a callback in its own process.

## The control program

`repro/exit_hang_repro.c` builds one small executable without the C runtime (`build.cmd`, or the
`cl` line at the top of the file). In modes 0 to 3 its entry point returns `0x60d` with a plain
`return`, no `ExitProcess`, in a known state:

| Mode | State when the entry point returns | Result on our machine (10 of 10 launches each) |
| --- | --- | --- |
| `0` | default pool, worker already idle | exits with `0x60d` (the control that must pass) |
| `1` | default pool, worker still inside a 3 s callback | **does not exit**: one `TppWorkerThread` left; once the callback ends, the idle worker still keeps the process alive |
| `2` | private pool with a minimum of 2 threads, idle | **does not exit**: two `TppWorkerThread`s left |
| `3` | an ordinary thread sleeping 3 s | exits after ~3 s with code **0**, not `0x60d`: the last thread's code wins |
| `4` | starts a child that inherits stdout/stderr, then `ExitProcess(0x60d)` | the process is gone, the pipes stay open ~5 s |

```bat
build.cmd
python exit_hang_probe.py --runs 10 --timeout 2 --linger 8 -- build\exit_hang_repro.exe 1
```

(In PowerShell, run `.\build.cmd`.) Under the probe, mode `1` takes about 10 s per launch
(`--timeout` plus `--linger`). Started by hand from a console, modes `1` and `2` stay running
until you close them: they are the hangs.

Use mode `0` and mode `1` as a pair whenever you change the probe or your environment: `0` must
give "0 hung" and `1` must give a hang with only thread-pool workers left.

## Where it comes from

The probe and the controls were first written for an intermittent hang in
[Xbox2Windows #281](https://github.com/R0Y00/Xbox2Windows/issues/281): a test program without the
C runtime finished its work and returned from its entry point, but on some machines, once every
few dozen runs, the process never exited and its pipes never closed.

1. On a machine where the hang did not occur, the probe ran the program 2,200 times without a
   single hang, and the controls showed that a thread-pool worker still alive when the entry
   point returns reproduces the reported end state on demand
   ([our comment](https://github.com/R0Y00/Xbox2Windows/issues/281#issuecomment-7070761909)).
2. On a machine where the hang does reproduce,
   [@abilliontokens](https://github.com/abilliontokens) then took the thread-level capture we had
   proposed. It hung at launch 100 and matched mode 1 exactly: the initial thread had exited and
   one idle `TppWorkerThread` kept the process alive
   ([comment](https://github.com/R0Y00/Xbox2Windows/issues/281#issuecomment-7071165027)).
3. The maintainer cleared the fix on that evidence
   ([comment](https://github.com/R0Y00/Xbox2Windows/issues/281#issuecomment-7071217988)), and
   [PR #394](https://github.com/R0Y00/Xbox2Windows/pull/394) by
   [@RonLogic](https://github.com/RonLogic), "cpu: terminate the native EXE after its entry
   returns", was merged.

This repository is a generic rewrite of that probe; it contains no code from Xbox2Windows. As a
check, the current Xbox2Windows test program (`cpu_exe_original.exe`, built from `main` with the
fix) gave 200 of 200 launches finished, exit code `0x60d`, with 8 in flight.

## Limits

- It shows **what** keeps a process alive, not **who** queued the work: a thread-pool worker's
  start address is always `TppWorkerThread`, and a C runtime thread's start address is the
  runtime's, not the thread function. For that, raise `--linger`, take a dump of the hung
  process meanwhile (Task Manager or `procdump -ma PID`) and look at the stacks in a debugger.
- Thread start addresses are read with `NtQueryInformationThread`; on a protected process or a
  process of another user it may fail (`--pid` then needs an elevated prompt, or cannot work).
- For launches, the processes it started are read from its job object, grandchildren included.
  With `--pid` they are found by parent process ID, which Windows does not reference-count: a
  match is a lead, not proof, and grandchildren are not listed.
- On a heavily loaded machine the inspection itself can take a second or more after the
  timeout (the report prints when it happened); a thread that ends in that window shows as
  "ended while being inspected".
- Tested on Windows 11 x64 with Python 3.12 only. Windows 10, older Pythons, 32-bit targets and
  ARM64 have not been tried.
- It only reads: it does not inject code or close handles in the target. What it does terminate:
  each launch runs in its own job object, and whatever is still running in that job is
  terminated (`TerminateJobObject`, or the job closing if the probe itself is killed): after
  `--linger` for a hung launch, right away for a launch that finished but left processes behind
  (the summary says so), and on Ctrl+C. Processes that the program asks someone else to start
  (a service, a scheduled task, an already running server) are not in its job and are not
  touched. In `--pid` mode nothing is terminated.

## Changelog

- **1.1.1**: a `--json` path that cannot be written (missing folder, a folder, a read-only
  file) is now a usage error (exit code `2`) found before the first launch, instead of a
  traceback with exit code `1` after the run; the file is written as launches finish
  (`"partial": true` until the end) and replaced in one step, so a killed run keeps its data;
  a reader that keeps the file open no longer turns the run into exit code `2` (updates are
  skipped with a warning, the final file is written in place). A second Ctrl+C that arrives while the first is still terminating the launches now always
  exits with `130`.
- **1.1.0**: each launch runs in its own job object (children and grandchildren are terminated
  too); Ctrl+C stops new launches and terminates those in flight; limits on every option, and
  usage errors exit with `2`.
- **1.0.0**: first release.

## Credits

Made by Roan System — https://roansystem.com · more tools and contributions on
[github.com/roansystemlabs](https://github.com/roansystemlabs). AI-assisted: the code and text
were prepared with an AI coding agent, and every output above was produced by running the probe
on a real Windows machine.

## License

MIT, see [LICENSE](LICENSE).
