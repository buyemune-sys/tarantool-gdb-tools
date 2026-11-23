# gdb gcore loses the thread stacks with glibc 2.42+ (issue #24)

**Status: not reported yet.** The bug in gdb is already fixed upstream, in gdb 18.1. What is left to file is a request to Ubuntu to backport the fix, plus a short note on the upstream bug. The decision is in [#24](https://github.com/buyemune-sys/tarantool-gdb-tools/issues/24).

## What happens

With gdb 17.1 on a recent distribution, `gcore` writes the stack of the main thread only. On the core, every other thread looks like this:

```
#1  0x0000000000000000 in ?? ()
```

Its stack, its TLS and the `struct pthread` at its top are zeroes, so thread-local variables read as 0. For Tarantool this loses the WAL, iproto, applier and coio threads. gcore prints only one warning per lost mapping, and nothing says that threads are gone:

```
warning: Memory read failed for corefile section, 1048576 bytes at 0x7abfe19fc000.
```

## Who is affected

A process is affected when it was started with glibc 2.42 or later on Linux 6.13 or later, and the core is written with gdb older than 18.1. The kernel's own core (`ulimit -c`, a crash, `kill -QUIT`) is not affected.

| Release | glibc | gdb | Affected |
|---|---|---|---|
| Ubuntu 24.04 LTS | 2.39 | 15.1 | no, even on a 7.0 kernel |
| Ubuntu 25.10 | 2.42 | 16.3 | yes |
| Ubuntu 26.04 LTS | 2.43 | 17.1 | yes |
| Ubuntu 26.10, devel | 2.44 | 17.2 | yes |
| Debian unstable | — | 18.1 | no (fixed gdb) |

These are the versions published on Launchpad as of 2026-11-23.

## Cause

- **glibc.** Since 2.42 (commit a6fbe36b7f, "nptl: Add support for setup guard pages with MADV_GUARD_INSTALL"), glibc installs the guard page of a thread stack with `madvise(MADV_GUARD_INSTALL)` inside the stack mapping, at its start. It used to be a separate `PROT_NONE` mapping.
  - Neighbouring stacks merge into one `rw-p` mapping.
  - `/proc/PID/smaps` shows the flag `gu`; `/proc/PID/pagemap` sets bit 58 (`PM_GUARD_REGION`) on the guard pages.
  - A read of a guard page through `/proc/PID/mem` fails with `EIO`.
- **gdb.** `gcore_copy_callback()` in `gdb/gcore.c` (gdb 17.1) copies a mapping in 1 MiB reads. At the first failed read it warns and stops copying that mapping. The guard page is at offset 0, so nothing of the mapping is written.
- **The upstream fix** is gdb commit c1da013915, "gcore: Handle unreadable pages within readable memory regions" (sourceware bug 33855). A failed 1 MiB read is retried page by page, and only the unreadable pages stay zeroes.
  - It is in gdb 18.1, not in 17.1 or 17.2.
  - The change is local to `gcore_copy_callback()`, about 40 lines, and applies the same way to the 16.3, 17.1 and 17.2 sources.

## Reproduction

[`repro/gcore-guard-pages.c`](repro/gcore-guard-pages.c) starts four threads that keep a marker on their stacks:

```
cc -O0 -g -pthread -o gcore-guard-pages docs/repro/gcore-guard-pages.c
./gcore-guard-pages > out & sleep 1
gdb -batch -p "$(awk '/READY/ {print $2}' out)" -ex 'gcore core.test'
grep -a -o 'STACK-MARK-T[1-4]' core.test | sort -u
gdb -batch ./gcore-guard-pages core.test -ex 'thread apply all bt'
```

Measured on Ubuntu 26.04.1 (Linux 7.0.0-43-generic, glibc 2.43-2ubuntu2.7, x86_64), on one running process:

| Core written by | Markers in the core | `bt` of threads 2–5 |
|---|---|---|
| gdb 17.1-2ubuntu1 `gcore` | 0 of 4 | `#1 0x0000000000000000 in ?? ()` |
| gdb 18.1, built from the release tarball | 4 of 4 | full stacks |
| gdb 17.1 with [`gcore-pages`](../README.md#gcore_pages_gdbpy) | 4 of 4 | full stacks |

None of these settings help with gdb 17.1:
- `set use-coredump-filter off`;
- `set dump-excluded-mappings on`;
- the glibc tunables `glibc.mem.decorate_maps`, `glibc.pthread.stack_hugetlb` and `glibc.pthread.stack_cache_size`. glibc has no tunable for this guard.

`process_madvise(MADV_GUARD_REMOVE)` cannot remove the guards from another process (`EINVAL`).

## Workarounds

- **`gcore-pages FILE`** from `gcore_pages_gdb.py`. It runs `gcore`, then reads again, page by page, what `gcore` left out, as gdb 18.1 does. It does not touch the process, and on gdb 18.1 or later it is plain `gcore`.
- **The kernel's core.**
- **gdb 18.1.**

CI shows the problem on every Ubuntu it runs: the `table` job of `cores.yml` has a row per runner, with OS, kernel, glibc, gdb and whether each kind of core keeps the stacks. `test_cores.test_gcore` is an expected failure exactly where the guard pages meet a gdb older than 18.1.

## What to file

### 1. Launchpad, package gdb (Ubuntu): a backport request

**Title:** gcore writes the stacks of all threads but the main one as zeroes (glibc 2.42+ MADV_GUARD_INSTALL guard pages); please backport upstream commit c1da013915

The text follows the stable release update template.

**[Impact]**

On Ubuntu 25.10, 26.04 LTS and 26.10, a core written by gdb's `gcore` of a multithreaded process holds the stack of the main thread only. Every other thread's stack, with its TLS and `struct pthread`, is zeroes, so `bt` in those threads stops after one frame. A core taken to debug a hang or a production problem silently lacks the threads that matter.

The cause is the guard pages that glibc 2.42 installs with `MADV_GUARD_INSTALL` inside the stack mapping, together with `gcore_copy_callback()` giving up on a mapping at its first failed read. Upstream fixed it in c1da013915 (bug 33855), in gdb 18.1.

**[Test Plan]**

1. Build and run the attached `gcore-guard-pages.c`, and write a core with `gcore`, as in the commands above.
2. Broken: the core has no `STACK-MARK-T` marker, and threads 2–5 show `#1 0x0000000000000000 in ?? ()`.
3. Fixed: all four markers are in the core, every thread has its full backtrace, and the only warning left is the one for the vsyscall page (`4096 bytes at 0xffffffffff600000`), as before.

**[Where problems could occur]**

Only gcore's copy loop changes, and only after a 1 MiB read has already failed. The chunk is then read page by page, and unreadable pages are written as zeroes instead of the rest of the mapping being dropped.
- A mapping that is unreadable as a whole still gets the warning.
- A process with very many unreadable pages costs up to 256 reads per failed MiB.
- Live debugging, reading cores and gdbserver are untouched.

**[Other Info]**

The bug needs glibc 2.42 or later and Linux 6.13 or later at the time the thread was created. Ubuntu 24.04 is not affected. Until the backport, the workarounds are the kernel's core, gdb 18.1, or a script that reads the failed mappings again after gcore (`gcore_pages_gdb.py` of github.com/buyemune-sys/tarantool-gdb-tools).

### 2. sourceware bug 33855: a comment

The bug is ASSIGNED with no target milestone, although c1da013915 is in gdb 18.1: the gdb-18.1-release tag contains it, gdb-17.2-release does not. The comment suggests setting the target milestone to 18.1 and closing the bug, so that distributions can see which release has the fix. It also names the affected Ubuntu releases.

## Recommendation

File both. The text and the reproducer are ready, the fix is small and already upstream, and it helps every user of gdb on Ubuntu 25.10 to 26.10, not only Tarantool. Filing takes a Launchpad account and about 15 minutes. After that, add the Launchpad link to #24.
