# Vendored upstream tests

Copies of the upstream tests of the gdb extensions, byte for byte, run
against tarantool through our own driver; never edited here. Re-sync
with `sync.sh` (it shows the diff, `--write` takes it).

| File | Source | Commit | License |
|---|---|---|---|
| `luajit/debug-extension-tests.py` | tarantool/luajit `test/tarantool-debugger-tests/debug-extension-tests.py`, branch `elhimov/gh-13094-dbg-avoid-hardcoded-enums` (Mikhail Elhimov) | `c4cf7f367fe709f032e7e7f176379579603fff1e` | MIT (LuaJIT, see `COPYRIGHT` of tarantool/luajit) |

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
own hook check, and before 3.7 the first GC cycle runs before
`tarantool_L` is set. `tests/dbg/test_luajit.py` checks the same
commands in the form tarantool allows, live and on cores. The runner
gives a gdb run 60 s (`DBG_UPSTREAM_TIMEOUT`) instead of the upstream
10, and tells `TestLoad` about `lj-gctop`, not upstream yet.
