# lldb cannot allocate memory in a process with a pending signal (issue #25)

**Status: not reported yet.** This is a bug in lldb-server on Linux, still in LLVM main. `luajit_gdb.py` works around it. The decision is in [#25](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/25).

## What happens

In a stopped process that has a signal pending, every lldb expression that needs memory in the inferior fails. That includes calling a function, a result that does not fit a register, and a persistent variable:

```
(lldb) expression -- (int)stop_here(41)
error: Couldn't materialize: couldn't allocate a temporary region for the result: memory write failed for 0x29
error: errored out in DoExecute, couldn't PrepareToExecuteJITExpression
```

Tarantool always has a signal pending when it is stopped: its low-resolution clock is a 10 ms `ITIMER_REAL`, so a `SIGALRM` is due every time lldb stops it.

## Reproduction

[`repro/lldb-pending-signal.c`](repro/lldb-pending-signal.c) runs a 10 ms `ITIMER_REAL`. Built with `-DNO_TIMER`, the same program has no timer:

```
cc -O0 -g -o lldb-pending-signal docs/repro/lldb-pending-signal.c
cc -O0 -g -DNO_TIMER -o lldb-pending-signal-notimer docs/repro/lldb-pending-signal.c
lldb -b -o 'breakpoint set -n stop_here' -o run \
     -o 'expression -- (int)stop_here(41)' ./lldb-pending-signal
```

Measured on Ubuntu 26.04.1 x86_64, with the lldb packages of Ubuntu 26.04:

| lldb | With the timer | `-DNO_TIMER` |
|---|---|---|
| 22.1.2 | `memory write failed for 0x29` | `(int) $0 = 41` |
| 18.1.8 | `memory write failed for 0x29` | `(int) $0 = 41` |

`process handle SIGALRM --pass false --stop false --notify false` before `continue` does not help: the expression fails the same way. That setting only keeps `next` and `step` from stopping where they began.

## Cause

`NativeProcessLinux::AllocateMemory()` gets the memory by running `mmap` inside the inferior through `NativeProcessLinux::Syscall()`. The file is `lldb/source/Plugins/Process/Linux/NativeProcessLinux.cpp`, as of LLVM main 58ccd3c64369 (2026-10-23). `Syscall()` works like this:

1. It writes a syscall instruction at an executable address and sets the registers. On x86_64 the syscall number, 9 for `mmap`, goes into `rax`, which is also the result register.
2. It resumes the thread with `PTRACE_CONT`, or `PTRACE_SINGLESTEP` where the hardware supports it.
3. It waits with `waitpid(thread, &status, __WALL)` and reads the result register. It does not look at `status`.

With a signal pending, the first stop is the signal-delivery stop of `SIGALRM`, before the syscall instruction runs. The result register still holds the syscall number. 9 is below the errno threshold, so `Syscall()` returns it as the address, and lldb then tries to use memory at that address ("memory write failed for 0x29").

## Suggested fix

In `Syscall()`, after `waitpid()`, check that the thread stopped with `SIGTRAP`.
- If the stop is a signal-delivery stop of another signal, remember the signal, resume the thread without delivering it, and wait again until the trap of the syscall instruction.
- After the registers are restored, leave the remembered signal pending again, so the inferior loses nothing.

That is roughly 15 lines in one function. `PTRACE_SETSIGINFO` already restores the siginfo there.

## The workaround in luajit_gdb.py

`_prime_allocations()` runs once per process, before the first expression:
- It asks lldb for memory of each permission set it needs, until the returned region is really mapped.
- It frees the good allocation and leaves the bad ones reserved, so lldb's allocation cache never hands them out.

The README tells users to add `process handle SIGALRM --pass false --stop false --notify false`, for stepping. Expressions that users type themselves still fail before the extension has primed the process.

## What to file

An issue at [llvm/llvm-project](https://github.com/llvm/llvm-project/issues), labelled `lldb`.

**Title:** [lldb] Linux: AllocateMemory returns the mmap syscall number when a signal is pending

**Body:**
- the reproducer and the table above;
- the cause: `NativeProcessLinux::Syscall()` takes the first `waitpid()` stop and reads the result register without checking for `SIGTRAP`;
- the suggested fix.

A patch with a test is welcome there. The test would be an API test that arms an `ITIMER_REAL` in the inferior and evaluates an expression that allocates.

## Recommendation

Report it. The reproducer is 40 lines with no dependencies, the cause is pinned to one function, and every lldb user debugging a program with an interval timer or a busy signal handler hits it. That includes any program that uses `setitimer` or `timer_create` for a clock. Filing takes about 20 minutes. Sending the fix as a pull request would take longer, because the lldb test suite has to build and run. After filing, add the link to #25.
