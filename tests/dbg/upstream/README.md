# Vendored upstream tests

Copies of the upstream tests of the gdb extensions, byte for byte, run
against tarantool through our own driver; never edited here. Re-sync
with `sync.sh` (it shows the diff, `--write` takes it).

| File | Source | Commit | License |
|---|---|---|---|
| `luajit/debug-extension-tests.py` | tarantool/luajit `test/tarantool-debugger-tests/debug-extension-tests.py`, branch `elhimov/gh-13094-dbg-avoid-hardcoded-enums` (Mikhail Elhimov) | `c4cf7f367fe709f032e7e7f176379579603fff1e` | MIT (LuaJIT, see `COPYRIGHT` of tarantool/luajit) |
| `tarantool/conftest.py`, `test_gdb.py`, `utils.py`, `ut_tt_fiber.py`, `ut_tl_var.py` | tarantool/tarantool `test/gdb/`, PR #13078, branch `elhimov/gh-13076-add-fiber-switch-tests` (Mikhail Elhimov) | `fa8bd1760306454649f128e261a37bfa48fc53b8` | BSD-2-Clause (Tarantool, see `LICENSE` of tarantool/tarantool) |
| `tarantool/test-tarantool-gdb.py` | tarantool/tarantool `tools/test-tarantool-gdb.py`, PR #9665 (Mikhail Elhimov) | `4f7e2fff955286f0373c5e4ecba86985fec78d38` | BSD-2-Clause (Tarantool) |

## LuaJIT

The upstream file runs gdb (or lldb) against the luajit binary.
`run_luajit.py` runs it unchanged against tarantool: it sets
`LUAJIT_TEST_BINARY` to `$LJBT_TARANTOOL`, `DEBUGGER_EXTENSION_PATH` to a
directory with the extension under test (`$DBG_LUAJIT_DBG`, default the
`luajit_dbg.py` of this repo) and `DEBUGGER_COMMAND` to `gdb-tarantool`:
gdb with `tarantool.gdb`, whose `hook-run` puts
`-e "jit.opt.start('hotloop=56') jit.flush()"` in front of the inferior
arguments: the first trace of a test script is trace 1 as in plain
LuaJIT, and no Lua of tarantool's startup is about to become hot.

```
LJBT_TARANTOOL=<tarantool with DWARF> python3 tests/dbg/upstream/run_luajit.py
LJBT_TARANTOOL=... DBG_LUAJIT_DBG=<dir>/luajit-gdb.py python3 tests/dbg/upstream/run_luajit.py
```

Tests that cannot pass against tarantool whatever the extension does
are skipped with the reason (`tarantool_skips()` in `run_luajit.py`):
tarantool's `print` is a Lua wrapper, its startup Lua reaches the
breakpoints first, it declares `getpid()`, every trace starts with its
own hook check, before 3.7 the first GC cycle runs before
`tarantool_L` is set, and the arm64 builds of 3.2, 3.3, 3.5 and 3.6
have `fname` of `lj_cf_dofile` optimized out (`TestLJStr`).
`tests/dbg/test_luajit.py` checks the same commands in the form
tarantool allows, live and on cores. The runner gives a gdb run 60 s
(`DBG_UPSTREAM_TIMEOUT`) instead of the upstream 10, tells `TestLoad`
about `lj-gctop`, not upstream yet, and lets the lj-stack tests see any
value in the red zone: LuaJIT puts metamethod frames there and clears
it only in the atomic GC phase.

`DBG_DEBUGGER=lldb` runs the same file in its lldb mode, through
`lldb-tarantool` (lldb, or `$DBG_LLDB`): it puts the same options in
the `settings set target.run-args` line of the commands of each test,
and does not pass SIGALRM to tarantool (`process handle`): the 10 ms
timer of tarantool's low-resolution clock keeps one pending in a
stopped process, and lldb's `n` then stops where it began. The upstream
file skips the deferred loading in lldb; two more tests are skipped
there, both for where lldb puts a breakpoint by name: at the entry of
`lj_cf_dofile`, where `fname` is not set yet after one `n`
(`TestLJStr`), and, for the inlined `gc_sweep_str_chain`, at the start
of its range in the cold part of `gc_onestep`, never reached
(`TestGCStateSweepString`).

```
LJBT_TARANTOOL=... DBG_DEBUGGER=lldb DBG_LLDB=lldb-22 python3 tests/dbg/upstream/run_luajit.py
```

## Tarantool

`test_gdb.py` of PR #13078 is a pytest file: each test runs gdb with
the extension and one of `ut_tt_fiber.py` (`tt-fiber`, `info tt-fibers`
on every fiber) or `ut_tl_var.py` (`$tl_var_addr`, `$tl_var_find_offset`,
`$tl_var_like` on every thread), live at `run_script_f` and on the core
of a crash. `test-tarantool-gdb.py` of PR #9665 is a gdb script: it
encodes every MsgPack type and extension into a string literal and
checks what `tt-mp` prints; `tt_mp_test.py` runs it as a pytest test,
live (a literal needs a process to live in).

`run_tarantool.py` runs both with pytest (`pip install pytest`, or
`DBG_PYTHON=<python3 with pytest>`). It does not load
`tarantool/conftest.py` (`--noconftest`): the plugin `tt_fixtures.py`
gives the same fixtures, with `tarantool_bin` from `$LJBT_TARANTOOL`,
`tarantool_gdb_ext` from `$DBG_TT_GDB` (default the `tarantool-gdb.py`
of this repo) and `tarantool_coredump` written by gdb's `gcore` at the
SIGSEGV of the same crash, so the core tests need neither a
`core_pattern` nor `ENABLE_COREDUMP_TESTS`.

```
LJBT_TARANTOOL=<tarantool with DWARF> python3 tests/dbg/upstream/run_tarantool.py
LJBT_TARANTOOL=... DBG_TT_GDB=<dir>/tarantool-gdb.py python3 tests/dbg/upstream/run_tarantool.py -k fiber
```
