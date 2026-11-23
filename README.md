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
looks at one that is already running) and, when it does not finish in time, records what is
still keeping it alive, *before* anything is killed:

- is the process object signaled, i.e. has the process really ended?
- did its stdout/stderr pipes reach end-of-file, or does another process still hold them?
- which threads are still in it: start address as `module+offset`, whether each one is the
  initial thread, a default thread-pool worker (`ntdll!TppWorkerThread`), a C runtime thread or
  a thread of the program's own code, plus CPU time and age;
- which child processes it left running, and which modules are loaded.

Then it keeps watching for `--linger` seconds to see whether the process ends by itself, and
terminates it only if it is still alive. One Python file, standard library only. A tiny C
program with deterministic hang-at-exit cases is included as a control.

## When to use it

- A test or CI step times out "sometimes" because a child process finishes its work but never
  exits, or its output pipe never closes.
- A tool exits fine on your machine and hangs on someone else's: run it a few hundred times
  there with `--runs` and `--parallel`, and the first hang comes with the thread list.
- A process is stuck right now: `--pid` gives the same report without launching anything.

## Requirements

- Windows 10 or 11, Python 3.9 or later (standard library only).
- Only for the optional control program: MSVC (Visual Studio or Build Tools with the C++
  workload).

## Usage

```bat
python exit_hang_probe.py --runs 200 --parallel 8 --timeout 5 -- my_tool.exe --check
python exit_hang_probe.py --pid 12345
```

| Option | Meaning |
| --- | --- |
| `-- PROGRAM [ARGS...]` | the program to launch (a path, or a name found on `PATH`) |
| `--pid PID` | inspect a process that is already running, then exit |
| `--runs N` | launches (default 20) |
| `--parallel K` | launches in flight at the same time (default 1); load often makes rare hangs show up |
| `--timeout S` | seconds before a launch counts as hung (default 5) |
| `--linger S` | seconds to keep watching a hung process before terminating it (default 30) |
| `--priority normal\|below` | priority class of the launched program |
| `--stdin-hex HEX` | bytes written to stdin (default: none, stdin is closed at once) |
| `--show N` | hung launches printed in full (default 3) |
| `--json FILE` | every launch, the summary and all thread data as JSON |
| `--no-banner` | skip the start-up header (it is also skipped when the output is redirected) |

A launch counts as finished only when the process has ended **and** both pipes reached EOF
within the timeout. Exit codes: `0` no hang, `1` at least one launch hung, `2` usage error or a
process that cannot be opened.

## Example output

All output below is real, from Windows 11 (build 26200) and Python 3.12.

A program whose entry point returns while a thread-pool worker is still alive (control mode 1,
see below). The initial thread is gone and one `TppWorkerThread` keeps the process open:

```text
exit_hang_repro.exe: 3 runs (1 in flight, normal priority), 0 finished within 2 s, 3 hung

hung launch 1/3: pid 57372, inspected 2.13 s after launch
  process signaled: no (still alive)
  stdout EOF: no (0 bytes)   stderr EOF: no (0 bytes)
  threads still in the process: 1
    tid 56136  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 5.0 ms after the process, cpu 0.0 ms
  modules: exit_hang_repro.exe, ntdll.dll, KERNEL32.DLL, KERNELBASE.dll
  hint: the initial thread is gone: the entry point returned or called ExitThread, and the process lives on through its remaining threads
  hint: only thread-pool workers are left (1); an entry point that returns instead of calling ExitProcess waits for them. Who queued the work is not visible here
  then: still alive with 1 thread(s), terminated by the probe after 10.22 s

(2 more hung launches not shown; use --show or --json)
```

A program that has already exited, but a child it started inherited its stdout/stderr, so the
caller's read never reaches EOF (control mode 4):

```text
hung launch 1/2: pid 37104, inspected 2.1 s after launch
  process signaled: yes (the process has ended)
  stdout EOF: no (0 bytes)   stderr EOF: no (0 bytes)
  exit code: 1549 (0x60d)
  child processes still running: exit_hang_repro.exe (pid 50220)
  hint: the process has ended but its stdout/stderr pipe is still open: another process inherited the pipe and still holds it; children still running: exit_hang_repro.exe (pid 50220)
  then: stdout/stderr reached EOF after 5.04 s
```

An ordinary Python script that prints its result and then starts a non-daemon thread it never
joins. The interpreter's main thread waits for it at shutdown:

```text
python.EXE: 3 runs (1 in flight, normal priority), 0 finished within 2 s, 3 hung

hung launch 1/3: pid 60816, inspected 2.12 s after launch
  process signaled: no (still alive)
  stdout EOF: no (11 bytes)   stderr EOF: no (0 bytes)
  threads still in the process: 5
    tid 58504  python.EXE+0x12a0            initial thread (executable entry point)
               running, started 0.0 ms after the process, cpu 15.6 ms
    tid 32796  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 5.7 ms after the process, cpu 0.0 ms
    tid 60436  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 5.7 ms after the process, cpu 0.0 ms
    tid 47500  ntdll.dll+0x24990            thread-pool worker (ntdll TppWorkerThread)
               running, started 5.8 ms after the process, cpu 0.0 ms
    tid 12636  ucrtbase.dll+0x2cd00         thread started by ucrtbase.dll
               running, started 24.4 ms after the process, cpu 0.0 ms
  modules: python.EXE, ntdll.dll, KERNEL32.DLL, KERNELBASE.dll, ucrtbase.dll, python312.dll, VCRUNTIME140.dll, WS2_32.dll, RPCRT4.dll, VERSION.dll, ADVAPI32.dll, msvcrt.dll (+4 more)
  hint: the initial thread is still running: main() has not returned, or the runtime is waiting at shutdown (for example joining a thread that never ends)
  hint: some threads were started by ucrtbase.dll, the C runtime's thread start (_beginthreadex, std::thread, language runtimes): the program created them, and the real thread function is not visible from the start address
  then: ended by itself after 8.06 s, exit code 0 (0x0)
```

A healthy program gives one summary line:

```text
cmd.EXE: 20 runs (1 in flight, normal priority), 20 finished within 5 s, 0 hung
  exit codes: 0x3 x20; slowest 74.0 ms
```

The `ntdll.dll+0x...` offset of `TppWorkerThread` differs between Windows builds; the probe
learns it at start-up by queueing a callback in its own process.

## The control program

`repro/exit_hang_repro.c` builds one small executable without the C runtime (`build.cmd`, or the
`cl` line at the top of the file). Its entry point returns `0x60d` with a plain `return`, no
`ExitProcess`, in a known state:

| Mode | State when the entry point returns | Result on our machine (10 of 10 launches each) |
| --- | --- | --- |
| `0` | default pool, worker already idle | exits with `0x60d` (the control that must pass) |
| `1` | default pool, worker still inside a 3 s callback | **does not exit**: one `TppWorkerThread` left |
| `2` | private pool with a minimum of 2 threads, idle | **does not exit**: two `TppWorkerThread`s left |
| `3` | an ordinary thread sleeping 3 s | exits after ~3 s with code **0**, not `0x60d`: the last thread's code wins |
| `4` | starts a child that inherits stdout/stderr, then `ExitProcess` | the process is gone, the pipes stay open ~5 s |

```bat
build.cmd
python exit_hang_probe.py --runs 10 --timeout 2 --linger 8 -- build\exit_hang_repro.exe 1
```

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
2. @abilliontokens then took the thread-level capture we had proposed on a machine where the
   hang does reproduce. It hung at launch 100 and matched mode 1 exactly: the initial thread had
   exited and one idle `TppWorkerThread` kept the process alive
   ([comment](https://github.com/R0Y00/Xbox2Windows/issues/281#issuecomment-7071165027)).
3. The maintainer cleared the fix on that evidence
   ([comment](https://github.com/R0Y00/Xbox2Windows/issues/281#issuecomment-7071217988)), and
   [PR #394](https://github.com/R0Y00/Xbox2Windows/pull/394) by @RonLogic, "cpu: terminate the
   native EXE after its entry returns", was merged.

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
- Child processes are found by parent process ID, which Windows does not reference-count: a
  match is a lead, not proof. Grandchildren are not listed.
- On a heavily loaded machine the inspection itself can take a second or more after the
  timeout (the report prints when it happened); a thread that ends in that window shows as
  "ended while being inspected".
- Tested on Windows 11 x64 only. 32-bit targets and ARM64 have not been tried.
- It only reads: it does not inject code or close handles in the target. The only action it
  takes is `TerminateProcess` on a launched program that is still alive after `--linger`
  (never in `--pid` mode).

## Credits

Made by Roan System — https://roansystem.com · more tools and contributions on
[github.com/roansystemlabs](https://github.com/roansystemlabs). AI-assisted: the code and text
were prepared with an AI coding agent, and every output above was produced by running the probe
on a real Windows machine.

## License

MIT, see [LICENSE](LICENSE).
