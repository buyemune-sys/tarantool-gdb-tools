# tarantool-gdb-tools

gdb extensions for debugging Tarantool and its LuaJIT, live and on core dumps, on top of the extensions Tarantool and LuaJIT ship: `luajit_dbg.py` (`luajit-gdb.py` in older LuaJIT) of LuaJIT (`lj-stack`, `lj-tv`, ...) and `tools/tarantool-gdb.py` of Tarantool (`info tt-fibers`, `tt-fiber`, ...). Each tool loads after them and reuses their helpers.

| Path | What |
|---|---|
| `lj_bt.py` | `lj-bt`: one stitched C + Lua backtrace, and the same for plain `bt` |
| `examples/` | scenarios and gdb scripts to try the tools on |
| `tests/` | automated tests, run against a Tarantool build with DWARF |

Status: prototypes. Everything needs DWARF in the Tarantool binary, as `luajit_dbg.py` does. The static deb of Tarantool's package repository carries it; the stripped static rpm that `tt binary install` puts does not, but the deb binary of the same version has the same build-id and serves as its symbol file (`symbol-file` or `file` in gdb, also for a core of the rpm build).

## lj-bt

`lj-bt` prints the C frames and the Lua frames of a Tarantool process in the real order of the calls. Plain `bt` shows only the C stack, and `lj-stack` shows only the Lua stack of one coroutine; matching them by eye is what this replaces. Loading it also installs a frame filter, so plain `bt` prints the stitched stack, and an unwinder, so `bt` goes through JIT traces.

Tested on x86_64 with the official static debs of Tarantool CE 3.3 to 3.9 (GC64, DWARF; see [CI](#ci)), and with Tarantool master built from source with DWARF, with and without GC64.

### Usage

```
(gdb) source <luajit-src>/src/luajit_dbg.py     # luajit-gdb.py in Tarantool 3.3-3.5
(gdb) source lj_bt.py
(gdb) lj-bt [-v] [-n N] [--tail-chain] [--no-tail] [<lua_State *>]
(gdb) lj-bt --all-fibers
```

Load the extension of the LuaJIT the binary was built with: `src/luajit_dbg.py` of tarantool/luajit at the commit of Tarantool's `third_party/luajit`, which is `src/luajit-gdb.py` before LuaJIT 5db8c217 (Tarantool 3.3-3.5). `lj-bt` takes only the helpers both have; the bytecode and IR names it needs come from the DWARF of the binary. `tests/fetch.sh` gets the right files for a release.

For fibers, load Tarantool's own extension after the process has started: `python import gdb.printing`, then `source <tarantool-src>/tools/tarantool-gdb.py`. After `tt-fiber <id>`, `lj-bt` prints the stack of that fiber.

- `bt -no-filters` is the raw gdb backtrace; `disable frame-filter global lj-bt` turns the filter off.
- C frames keep gdb's numbers; the Lua frames of a VM band are `#N.1`, `#N.2`, ...
- Lines marked `!!` are mismatches or heuristics, never silent guesses.

`examples/example.gdb` and `examples/mixed.lua` reproduce the cases below: `tarantool examples/mixed.lua <sort|pcall|coro|error|trace|sleep|tail> <on|off> <main|fiber>`.

### How the stacks are matched

1. A VM band is a gdb frame whose pc is in the VM code and whose sp is a cframe: the VM keeps `rsp == cframe` while it runs. `cframe_L(sp)` tells whose Lua stack runs there, and `cframe_prev` chains the VM entries of that `lua_State` (`lj_frame.h`, `CFRAME_OFS_L`, `CFRAME_OFS_PREV`).
2. The Lua frames of a `lua_State` are split between its cframes as `err_unwind` in `lj_err.c` does: a `FRAME_C`/`FRAME_CP` link closes the segment of a cframe and moves to the previous one; a cframe with `nres < 0` owns no frames.
3. A Lua C function on top of a segment is the C frame right above the band; Lua functions and fast functions have no C frame of their own. Fast functions are named by their ffid.
4. The current line of a Lua frame comes from the return PC its callee's frame link holds; the top frame of a segment uses `cframe_pc`, marked as possibly stale.
5. JIT traces: a pc inside a trace's mcode is mapped back to its band (`cframe = sp + spadjust + 16`, after `BC_JLOOP`); the inlined Lua frames come from the snapshot covering the pc, as `lj_trace_unwind` and `lj_snap_restore` pick and read it.
6. An inconsistent stack - the window in which sysprof crashes - is printed as far as it goes, with the mismatch marked: a cframe that owns no Lua frame yet, a clobbered frame link, and where sysprof's `top_frame_info.top_frame` points.

### Tail calls (heuristic)

`return f(x)` (`BC_CALLT`) replaces the caller's frame in place, and LuaJIT keeps no mark of it. The call site of the frame below still says what was called, as `lj_debug_funcname` names functions. `lj-bt` resolves that name - a global, an upvalue, a field or a method through `__index` tables, a live slot below the call base - to its current value. When that is not the function running, it prints `(tail call from <name> = <chunk:line>)  [heuristic]`. `--tail-chain` follows the static `CALLT`s of the function found, marked `probably`.

It is silent where the callee cannot be resolved (a call result, `__index` functions, `__call`, continuation frames, calls from C) and wrong when the global or field was reassigned after the call. Only the first function of a chain of tail calls is certain.

### Examples

A mixed stack: an FFI `get` called from a `table.sort` comparator, through a chain of tail calls.

```
#3    lj_cf_ffi_meta___call () at src/lib_ffi.c:230  <- builtin ffi.meta.__call
#4    lj_BC_FUNCC ()  [VM entry L=0x40000378 cframe=0x7ffff3080e70, main L]
#4.1  [lua] builtin/box/schema.lua:2155 in function <builtin/box/schema.lua:2148>
#4.2  [lua] (tail call from method 'get' = builtin/box/schema.lua:2338)  [heuristic]
#4.3  [lua] mixed.lua:108 in function <mixed.lua:107>
#4.4  [lua] (tail call from global 'lj_a' = mixed.lua:112)  [heuristic]
#4.5  [lua] mixed.lua:115 in function <mixed.lua:114>
#4.6  [lua] mixed.lua:131 in main chunk
#5    lua_pcall () at src/lj_api.c:1178
```

A C function called from a JIT trace; plain gdb stops at `??` there.

```
!! L=0x40000378: running a trace: Lua frames from g->jit_base 0x406480e8
#1    0x555580d127a7 in [trace 175] started at mixed.lua:84  snapshot #6
#1.1  [lua] mixed.lua:80 in function <mixed.lua:78>  (inlined in trace 175, snapshot #6)
#2.1  [lua] mixed.lua:85 in function <mixed.lua:73>  (trace 175 base frame, snapshot #6)
```

The sysprof crash (tarantool/tarantool#11184): a sample taken after an `on_replace` trigger returned to C, while `space:replace` is still running.

```
!! sysprof walks cur_L from top_frame_info.top_frame-1 = 0x40863348, the top frame is 0x40863330 (+3 slots)
#9    lj_vm_return ()  [VM entry L=0x404dda70 cframe=0x7fffc93809a0, fiber 118 "lua"]  !! owns no Lua frame: VM entry/exit in progress
#10   lua_pcall () at src/lj_api.c:1178
#14   space_run_replace_triggers () at box/space.c:962
#21   lbox_replace () at lua/index.c:85  <- C function lbox_replace  <called from a trace, stitched (lj_cont_stitch)>
#22   lj_BC_FUNCC ()  [VM entry L=0x404dda70 cframe=0x7fffc9380ec0, fiber 118 "lua"]
#22.1 [lua] prodlike.lua:54 in function <prodlike.lua:51>  <called from C: lua_pcall/cpcall>
```

### Known limitations

- In a trace, the frames are those of the last snapshot before the pc: the line may lag a line behind, a fresh inlined frame may be missing, and the function may be one that later tail-called further.
- Only x86_64 is tested; the layouts for arm64 are transcribed from `lj_frame.h` and never run.
- Lua tail calls are recovered by the heuristic above only; a tail call into C shows as a missing C frame.
- gdb's inline frames of `-O2` C code are printed as `[inlined]` and not otherwise examined.
- `tarantool_L` is shared by several fibers whose cframes lie on different C stacks; without `tarantool-gdb.py` such frames are marked, not attributed to a fiber.
- Not tried: ffi callbacks, hooks, the error handler of `xpcall`, `lua_cpcall` without a frame.

### Machine-readable output

`lj-bt --json [-o FILE]` prints the same backtrace as one JSON document: the frames with their Lua entries, the C function folded into each, the `!!` mismatches and the `--` notes, and per `lua_State` how many frame links were walked and hidden. The tests compare it rather than the text.

## Tests

`tests/run.sh [GROUP...]` runs the tests of `lj-bt` against a Tarantool build with DWARF:

| Group | What |
|---|---|
| `oracle` | fixed chains: the Lua frames must match what the scenario recorded with `debug.getinfo` right before the call, the C frames must match `bt -no-filters`, every frame link appears once, and there is no `!!` |
| `random` | chains generated from a seed out of 14 kinds of steps (call, tail call, pcall, xpcall, coroutine resume and wrap, `table.sort` comparator, `gsub` callback, `__index`, `__call`, `__add`, another fiber, a hot loop under JIT, an error with an `xpcall` handler), checked against the generator's model |
| `stops` | stops at random points of the VM - `SIGPROF` of sysprof in mode D, which walks no stack - on a mixed load: no Python error, the C frames kept, the outer frames found, every mismatch marked |
| `core` | the same chains on `gcore` files, with no live process: the output matches the live one |
| `tail` | the table of the tail call heuristic, positive and negative cases |
| `crash` | the sysprof crashes in mode C (tarantool/tarantool#11184): no Python error, `!!` with the shift of `top_frame`, the right Lua frame (long and racy) |
| `all` | everything; the default is all but `crash` |

```
LJBT_TARANTOOL=<tarantool with DWARF> \
LJBT_LUAJIT_DBG=<luajit-src>/src/luajit_dbg.py \
LJBT_TT_GDB=<tarantool-src>/tools/tarantool-gdb.py \
LJBT_SEEDS=200 LJBT_STOPS=500 tests/run.sh all
```

`luajit_dbg.py` must come from the same LuaJIT as the binary. A failed seed is replayed with `LJBT_SEED0=<n> LJBT_SEEDS=1 tests/run.sh random`. Temporary files go to `TMPDIR`; the box scenarios listen on unix sockets only. `stops` and `crash` need sysprof and skip themselves without it (arm64).

`tests/fetch.sh <version> <amd64|arm64> <dest>` downloads the official static deb of a release, unpacks it, adds the gdb extensions of its sources and prints the three variables, so a release is tested with:

```
export $(tests/fetch.sh 3.9.0 amd64 ~/tarantool-3.9.0)
tests/run.sh
```

## CI

`.github/workflows/test.yml` runs the tests against every Tarantool CE minor that tt v3 supports, the last patch of each, on both architectures:

| | 3.3.5 | 3.4.2 | 3.5.2 | 3.6.4 | 3.7.2 | 3.8.2 | 3.9.0 |
|---|---|---|---|---|---|---|---|
| LuaJIT extension | `luajit-gdb.py` | `luajit-gdb.py` | `luajit-gdb.py` | `luajit_dbg.py` | `luajit_dbg.py` | `luajit_dbg.py` | `luajit_dbg.py` |
| amd64 (`ubuntu-24.04`) | all groups | all groups | all groups | all groups | all groups | all groups | all groups |
| arm64 (`ubuntu-24.04-arm`) | oracle core random tail | same | same | same | same | same | same |

- The binaries are the static debs of Tarantool's package repository (they carry DWARF), cached per version and architecture; the extensions come from the tag (`tools/tarantool-gdb.py`) and from tarantool/luajit at the tag's `third_party/luajit`.
- On push and pull requests the long groups are cut: 50 seeds, 100 stops, two crash attempts of 20 s. The daily scheduled run takes 200 seeds, 500 stops, three attempts of 40 s.
- On arm64 LuaJIT has no sysprof (nor memprof), so `stops` and `crash` are skipped, with a notice in the log. The arm64 layouts of `lj-bt` were never run before this CI, so the arm64 jobs are `continue-on-error` until they are green.
- A failed job uploads its work directories (`gdb.log`, `probe.jsonl`, `violations.jsonl`) as the artifact `logs-<version>-<arch>`.

## License

BSD 2-Clause, see `LICENSE`.
