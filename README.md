# tarantool-gdb-tools

gdb extensions for debugging Tarantool and its LuaJIT, live and on core dumps, on top of the extensions Tarantool and LuaJIT ship: `luajit_dbg.py` (`luajit-gdb.py` in older LuaJIT) of LuaJIT (`lj-stack`, `lj-tv`, ...) and `tools/tarantool-gdb.py` of Tarantool (`info tt-fibers`, `tt-fiber`, ...). Each tool loads after them and reuses their helpers.

| Path | What |
|---|---|
| `lj_bt.py` | `lj-bt`: one stitched C + Lua backtrace, and the same for plain `bt` |
| `luajit_dbg.py` | LuaJIT's extension (`lj-stack`, `lj-tv`, ...), one file for every Tarantool 3.x, see [below](#luajit_dbgpy) |
| `tarantool-gdb.py` | Tarantool's extension (`info tt-fibers`, `tt-fiber`, `tt-mp`, `tt-list`, ...), one file for every Tarantool 3.x, see [below](#tarantool-gdbpy) |
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

Load the extension of the LuaJIT the binary was built with: `src/luajit_dbg.py` of tarantool/luajit at the commit of Tarantool's `third_party/luajit`, which is `src/luajit-gdb.py` before LuaJIT 5db8c217 (Tarantool 3.3-3.5). `lj-bt` takes only the helpers both have; the bytecode and IR names it needs come from the DWARF of the binary (if a build has no such enum, from the tables of `luajit_dbg.py`, with a `--` note; `luajit-gdb.py` has none, then `lj-bt` says so and stops). `tests/fetch.sh` gets the right files for a release.

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

Captured on Tarantool 3.9.0 (x86_64, the static deb), from the repository root.

A mixed stack: the memtx `box.space.t:get()` (FFI) at the end of a chain of tail calls, `lj_a -> lj_b -> lj_c` (`tarantool examples/mixed.lua tail on main`, stopped in `box_index_get` after the marker, as `examples/example.gdb` does).

```
lj-bt: L=0x7ffff7945378 (G->cur_L), G=0x7ffff79453d8, vmstate=CFUNC
#0    box_index_get () at box/index.cc:534
#1    lj_vm_ffi_call () at buildvm_x86.dasc:2867
#2    lj_ccall_func () at src/lj_ccall.c:1224
#3    lj_cf_ffi_meta___call () at src/lib_ffi.c:230  <- builtin ffi.meta.__call
#4    lj_BC_FUNCC ()  [VM entry L=0x7ffff7945378 cframe=0x7ffff6480e50, main L]
#4.1  [lua] builtin/box/schema.lua:2155 in function <builtin/box/schema.lua:2148>
#4.2  [lua] (tail call from method 'get' = builtin/box/schema.lua:2338)  [heuristic]
#4.3  [lua] examples/mixed.lua:110 in function <examples/mixed.lua:109>
#4.4  [lua] (tail call from global 'lj_a' = examples/mixed.lua:114)  [heuristic]
#4.5  [lua] examples/mixed.lua:117 in function <examples/mixed.lua:116>
#4.6  [lua] examples/mixed.lua:133 in main chunk  <called from C: lua_pcall/cpcall>
#5    lua_pcall () at src/lj_api.c:1142
#6    luaT_call () at lua/utils.c:755
#7    lua_main () at lua/init.c:1060
#8    run_script_f () at lua/init.c:1280
#9    fiber_cxx_invoke(fiber_func, typedef __va_list_tag __va_list_tag *) () at core/fiber.h:1348
#10   fiber_loop () at core/fiber.c:1187
#11   coro_init () at coro/coro.c:108
```

A C function called from a JIT trace (`mixed.lua trace on main`, `break box_space_id_by_name if len == 8`).  gdb alone stops at `??` there; with `lj_bt.py` loaded even `bt -no-filters` goes on through the trace's band.

```
lj-bt: L=0x7ffff7945378 (G->cur_L), G=0x7ffff79453d8, vmstate=TRACE 158
-- L=0x7ffff7945378: running a trace: Lua frames from g->jit_base 0x7ffff69c4af0
#0    box_space_id_by_name () at box/box.cc:3753
#1    0x2195fcfe in [trace 158] started at examples/mixed.lua:86  snapshot #6
#1.1  [lua] examples/mixed.lua:82 in function <examples/mixed.lua:80>  (inlined in trace 158, snapshot #6)
#2    lj_BC_JLOOP ()  [VM entry L=0x7ffff7945378 cframe=0x7ffff6480e50, main L]
#2.1  [lua] examples/mixed.lua:87 in function <examples/mixed.lua:75>  (trace 158 base frame, snapshot #6)
#2.2  [lua] examples/mixed.lua:133 in main chunk  <called from C: lua_pcall/cpcall>
#3    lua_pcall () at src/lj_api.c:1142
```

The sysprof crash (tarantool/tarantool#11184), `tests/lua/crash.lua trigger`: the sample is taken while the `on_replace` trigger, called from C under `space:replace` (itself called from a trace), pushes its arguments; sysprof's `top_frame` is stale, far off the top frame of the trigger's fiber.

```
lj-bt: L=0x7ffff6e5b7b8 (G->cur_L), G=0x7ffff79453d8, vmstate=CFUNC
!! sysprof walks cur_L from top_frame_info.top_frame-1 = 0x7ffff79485a8, the top frame is 0x7ffff69fdae8 (+2004312 slots)
#0    stream_backtrace_lua () at src/lj_sysprof.c:188 [inlined]
#1    stream_guest () at src/lj_sysprof.c:256
#4    sysprof_signal_handler () at src/lj_sysprof.c:337
#5    <signal handler called>
#6    clearhpart () at src/lj_tab.c:84 [inlined]
#11   lj_cdata_setfin () at src/lj_cdata.c:98
#12   luaL_setcdatagc () at lua/utils.c:429
#13   luaT_pushtuple () at lua/tuple.c:697
#14   port_c_dump_lua () at lua/misc.cc:303
#16   func_adapter_lua_call () at lua/func_adapter.c:59
#19   space_run_replace_triggers () at box/space.c:929
#24   box_replace () at box/box.cc:4043
#25   lbox_replace () at lua/index.c:85 [inlined]
#26   lbox_replace () at lua/index.c:70  <- C function lbox_replace  <called from a trace, stitched (lj_cont_stitch)>
#27   lj_BC_FUNCC ()  [VM entry L=0x7ffff6e5b7b8 cframe=0x7fffe1b80ed0]
#27.1 [lua] tests/lua/crash.lua:47 in function <tests/lua/crash.lua:44>  <called from C: lua_pcall/cpcall>
#28   lua_pcall () at src/lj_api.c:1142
```

The last example leaves frames out (the gaps in the numbers); every line shown is as lj-bt printed it.

### Known limitations

- In a trace, the frames are those of the snapshot that covers the pc, the one `lj_trace_unwind` would restore: the line may lag a line behind, and the function may be one that later tail-called further.  Inlined frames that no snapshot of the trace holds are not shown: LuaJIT takes a snapshot only where a guard needs one, and around a call from a trace the covering snapshot is the one after the call (`test_core.test_hop_snapshot_frames`: a loop calls two Lua functions inlined into the trace, the second makes an FFI call, and only the loop's frame is there).
- arm64 is checked by CI only. Its layouts come from `lj_frame.h` and `vm_arm64.dasc`: the VM's CFI there is based on the frame pointer, and `BC_JLOOP` lowers sp by 16 as on x64, which the trace unwinder takes into account. arm64 traces keep no trace number in `vmstate`, so the header says just `TRACE`. The register top of the stack (BASE/PC of the VM, x19/x21 there) is read on arm64 as well, untried outside CI.
- gdb stops at the bottom of a fiber stack with "previous frame identical" on arm64 (`coro_startup`, and `coro_transfer` with a pc 0 below for a fiber that has not run yet); that is a `--` note, not a mismatch.
- Lua tail calls are recovered by the heuristic above only; a tail call into C shows as a missing C frame.
- gdb's inline frames of `-O2` C code are printed as `[inlined]` and not otherwise examined.
- `tarantool_L` is shared by several fibers whose cframes lie on different C stacks; without `tarantool-gdb.py` such frames are marked, not attributed to a fiber.
- Not tried: hooks (the parser of `load()` under `lj_vm_cpcall` without a frame, FFI callbacks and the error handler of `xpcall` are steps of the test generator).

### Machine-readable output

`lj-bt --json [-o FILE]` prints the same backtrace as one JSON document: the frames with their Lua entries, the C function folded into each, the `!!` mismatches and the `--` notes, and per `lua_State` how many frame links were walked and hidden. The tests compare it rather than the text.

## Tests

`tests/run.sh [GROUP...]` runs the tests of `lj-bt` against a Tarantool build with DWARF:

| Group | What |
|---|---|
| `oracle` | fixed chains, one or more per kind of step of `random`: the Lua frames must match what the scenario recorded with `debug.getinfo` right before the call (lines and chunk names), the C frames must match `bt -no-filters`, every frame link appears once, and there is no `!!` |
| `random` | chains generated from a seed out of 26 kinds of steps (call, tail call, pcall, xpcall, coroutine resume and wrap, `table.sort` comparator, `gsub` callback, `__index`, `__call`, `__add`, another fiber, a hot loop under JIT, an error with an `xpcall` handler, an FFI callback from `qsort`, a vararg function, the reader of `load()` under the parser, a function of a `loadstring` chunk, tail calls whose head is called through an upvalue, a field, a local, a for-in iterator, `__call` or a reassigned global, a function of 300 or 70000 lines, a recursion up to 5000 deep), checked against the generator's model: the frames and lines, how each frame was called, its chunk, its `lua_State` and VM entry |
| `stops` | stops at random points of the VM - `SIGPROF` of sysprof in mode D, which walks no stack - on a mixed load: no Python error, the C frames kept, the outer frames found, the frames of the load called as its model says, every mismatch marked |
| `core` | the same chains on `gcore` files, with no live process: the output matches the live one, and a plain `bt` as the first command shows the same Lua frames (one chain stops in a JIT trace); at that stop on the live process, also `bt` after `thread N` and back |
| `tail` | the table of the tail call heuristic, positive and negative cases |
| `crash` | the sysprof crashes in mode C (tarantool/tarantool#11184), four workloads: no Python error, the shape of the crash of each (the frames from the signal frame on, the Lua lines, `!!` with the shift of `top_frame`), and the same output on a `gcore` taken at the `SIGSEGV` (long and racy) |
| `cli` | `lj-bt` driven through gdb commands as a user does: every flag and wrong flags, repeated calls, the state of gdb around them, `thread N` and the iproto thread, a suspended coroutine, more C frames than `-n`, long local and upvalue names, VM helpers and the VM top, `tt-fiber` with `stepi` and the next stop in another fiber |
| `env` | the surroundings: a LuaJIT extension missing names or not set up yet, a binary without DWARF (alone and with `symbol-file`), no enums or `lj_bc_mode` in DWARF, the walk limits, the bottoms of fiber stacks, other stop handlers, `examples/example.gdb` |
| `rust` | Lua and Rust modules calling each other (see [below](#the-rust-group)), live and on `gcore` files; needs cargo, skipped without it |
| `all` | everything; the default is all but `crash` |

```
LJBT_TARANTOOL=<tarantool with DWARF> \
LJBT_LUAJIT_DBG=<luajit-src>/src/luajit_dbg.py \
LJBT_TT_GDB=<tarantool-src>/tools/tarantool-gdb.py \
LJBT_SEEDS=200 LJBT_STOPS=500 tests/run.sh all
```

`luajit_dbg.py` must come from the same LuaJIT as the binary. A failed seed is replayed with `LJBT_SEED0=<n> LJBT_SEEDS=1 tests/run.sh random`. Temporary files go to `TMPDIR`; the box scenarios listen on unix sockets only. `stops` and `crash` need sysprof and skip themselves without it (arm64). Breakpoint conditions on Lua numbers go through `$ljbt_arg(L, n)` of `tests/probe.py`: arm64 LuaJIT is dual-number, an integer there is an int32 with a tag, and `->n` would read its bits as a double.

`tests/fetch.sh <version> <amd64|arm64> <dest>` downloads the official static deb of a release, unpacks it, adds the gdb extensions of its sources and prints the three variables, so a release is tested with:

```
export $(tests/fetch.sh 3.9.0 amd64 ~/tarantool-3.9.0)
tests/run.sh
```

### The rust group

`tests/rust` holds two Rust modules, each a cargo workspace of its own with its `Cargo.lock`. `tests/rust/build.sh` builds them into `tests/rust/target` (or `LJBT_RUST_TARGET`); the test runs it too, cargo keeps it incremental.

- `plain`: a Lua C module and an FFI function on the bare Lua C API, no crates, so it builds offline. Four profiles: `ljbt-debug` (release with debuginfo), `release` (symbols only), `ljbt-strip` (no symbols, the Rust frames are `??`), `ljbt-abort` (`panic = "abort"`).
- `tnt`: on the `tarantool` crate 11.0.0 (rustc 1.89 or newer): `tlua` calls into Lua, fibers started from Rust, a C stored procedure (`#[tarantool::proc]`).

`tests/lua/rust.lua` runs the scenarios: Lua -> Rust -> `lua_call` -> Lua -> Rust -> Lua -> a box C function; 30 levels of Lua <-> Rust recursion; a Lua error thrown through the Rust frames to a `pcall` (with `panic = "abort"` it aborts the process: the landing pad of the Rust frame calls `panic_cannot_unwind`); a Rust function called through FFI from a hot loop, in a JIT trace; a `tlua` function that calls Lua in a new Lua thread; the stored procedure through `box.func` and through an iproto `call`; a fiber started from Rust that sleeps in Lua and one that sleeps in Rust. The Lua frames are compared with `debug.getinfo`, the C frames with `bt -no-filters`, the VM bands and the Rust frames between them with the structure of the chain. The stops of the debuginfo build and of `tnt` are also written with `gcore`, and the cores must give the same output. Known errors of `lj-bt` are `expectedFailure` tests, the reason in a comment.

`box.func` loads a C module from a copy that it deletes right after `dlopen`: gdb on a core finds no file for it and has no symbols of its frames, unless `set solib-search-path` names a directory with the module under the name of the copy, `<package>.so`.

## CI

`.github/workflows/test.yml` runs the tests against every Tarantool CE minor that tt v3 supports, the last patch of each, on both architectures:

| | 3.3.5 | 3.4.2 | 3.5.2 | 3.6.4 | 3.7.2 | 3.8.2 | 3.9.0 |
|---|---|---|---|---|---|---|---|
| LuaJIT extension | `luajit-gdb.py` | `luajit-gdb.py` | `luajit-gdb.py` | `luajit_dbg.py` | `luajit_dbg.py` | `luajit_dbg.py` | `luajit_dbg.py` |
| amd64 (`ubuntu-24.04`) | all groups | all groups | all groups | all groups | all groups | all groups | all groups |
| arm64 (`ubuntu-24.04-arm`) | oracle core random tail rust | same | same | same | same | same | same |

- The binaries are the static debs of Tarantool's package repository (they carry DWARF), cached per version and architecture; the extensions come from the tag (`tools/tarantool-gdb.py`) and from tarantool/luajit at the tag's `third_party/luajit`.
- On push and pull requests the long groups are cut: 50 seeds, 100 stops, two crash attempts of 20 s. The daily scheduled run takes 200 seeds, 500 stops, three attempts of 40 s.
- On arm64 LuaJIT has no sysprof (nor memprof), so `stops` and `crash` are skipped, with a notice in the log. The arm64 layouts of `lj-bt` were never run before this CI, so the arm64 jobs are `continue-on-error` until they are green.
- The Rust modules of the `rust` group are built with the runner's Rust (stable is installed if it is missing or older than 1.89), cached per architecture together with the crates.
- A failed job uploads its work directories (`gdb.log`, `probe.jsonl`, `violations.jsonl`) as the artifact `logs-<version>-<arch>`.

## luajit_dbg.py

LuaJIT's gdb (and lldb) extension in one version for every Tarantool 3.x, from 3.0.2 to 3.9.0, instead of the one of each release (`luajit-gdb.py` with 7 commands up to 3.5, `luajit_dbg.py` with 15 from 3.6). It is `src/luajit_dbg.py` of tarantool/luajit at Mikhail Elhimov's branch `elhimov/gh-13094-dbg-avoid-hardcoded-enums`, which takes the enums from the inferior, with these changes on top, each a commit of its own:

- the red zone of `lj-stack` is taken from the stack: it is 7 slots, not 8, in the LuaJIT of 3.0, 3.1 and 3.4;
- a Lua frame is `[L]`, never `[LP]` (that P was a bit of the return PC);
- the `lits` operand of `lj-bc` is signed (`KSHORT -7`, not `65529`);
- with no `lua_State` (before Tarantool sets `tarantool_L`, and since 3.7 in every thread with no Lua) the commands say so instead of a Python error;
- if the debug info lacks an enum (it may leave out one used only through its constants, tarantool/tarantool#13094), the built-in members are taken, the same in the LuaJIT of every 3.x, with a note;
- `lj-gctop [N]`: the number and total size of the GC objects and the N largest, ported from the branch `elhimov/gh-noticket-gdb-gctop`.

```
(gdb) source luajit_dbg.py
(gdb) lj-stack
(gdb) lj-gctop 20
(lldb) command script import luajit_dbg.py
```

`lj-bt` works on top of it as on top of the extension of the release (the bytecode and IR tables are there for every version). lldb is not tested here.

### Tests

`tests/dbg/run.sh [GROUP...]` runs the tests of `luajit_dbg.py` and `tarantool-gdb.py` against a Tarantool build with DWARF:

| Group | What |
|---|---|
| `luajit` | every command, live and on the `gcore` file of the same stop; the outputs must match and agree with an oracle: `debug.getinfo` for functions and frames, `jit.bc` for the bytecode, `jit.dump` for the IR, registers and snapshots of a trace, `collectgarbage('count')` for `lj-gctop`, gdb expressions for addresses, sizes and GC fields (default) |
| `upstream` | LuaJIT's own debugger tests, vendored unchanged in `tests/dbg/upstream/` (see its README), run against tarantool; what cannot pass there is skipped with the reason |
| `tarantool` | `tarantool-gdb.py`: every command and printer, live and on a core, see [below](#tarantool-gdbpy) |
| `upstream-tarantool` | Tarantool's own tests of the extension, vendored unchanged (see `tests/dbg/upstream/README.md`), with pytest |
| `all` | all four |

```
export $(tests/fetch.sh 3.9.0 amd64 ~/tarantool-3.9.0)
tests/dbg/run.sh all
DBG_LUAJIT_DBG=$LJBT_LUAJIT_DBG tests/dbg/run.sh upstream     # the extension of the release
```

`DBG_LUAJIT_DBG` points the tests at another extension, to see a test of a fix fail before it. `DBG_KEEP=1` keeps the work directories in `TMPDIR` (the plan, the result and the gdb log of every run). No scenario of `luajit_dbg.py` calls `box.cfg` or listens on a port.

`.github/workflows/dbg.yml` runs the four groups for 3.0.2, 3.1.2, 3.2.4, 3.3.5, 3.4.2, 3.5.2, 3.6.4, 3.7.2, 3.8.2 and 3.9.0 on amd64 and arm64 (arm64 `continue-on-error` for now), and the upstream tests once more with the extensions of the release, for the step summary.

## tarantool-gdb.py

Tarantool's gdb extension in one version for every Tarantool 3.x, from 3.0.2 to 3.9.0, instead of `tools/tarantool-gdb.py` of each release. It is that file of tarantool/tarantool at Mikhail Elhimov's PR #13078 (master and his fixes: `gdb.printing` for gdb 7.6, the architecture for gdb 8, a fiber switch without libthread_db), with his MsgPack refactor of PR #9665 on top (`tt-mp` prints through the pretty-printers, any `print` option and string literals work; the tuple printer shows the tuple's data, which replaces `tt-tuple`), and these changes, each a commit of its own:

- the fiber switch: the unwinder tells a thread by its ptid (gdb aborted on `thread N` after `info tt-fibers` on a core) and gives the saved context of the selected fiber to frame level 0 only (the selection did not survive a flush of the frame cache); what `lj_bt.py` uses stays: `fiber()`, `Cord().fibers()`, `FiberUnwinder.instance().fiber()`, `set_fiber()`, `reset_fiber()`;
- thread-local variables gdb cannot read (a core with no libthread_db; gdb 16+ reads the main thread's only) are read at their offset from the thread pointer: `$tl_var_find_offset` reads the TLS itself (gdb's memory search gives wrong addresses past unreadable memory, and crashed on cores), takes the match closest to the thread pointer, looks as far as the TLS segment goes (`cord_ptr` is 197 KiB away in 3.7 and 3.8), above `$tpidr` on AArch64, and prints no debug lines; `$tl_var_addr` works;
- since 3.7 `tuple_formats` is thread-local: it is read when a tuple is printed (the file did not load before `run` or `core-file`), from the main thread where the current one has none;
- the printers of `decimal_t`, `struct tt_uuid` and `struct datetime` print the value (a Python traceback in every release), with the 3-digit units of decNumber; MsgPack decimals have their 76 digits since 3.5;
- the list table has the lists of the older releases (quorum gain/loss up to 3.5, `all_txs` and the vinyl writers up to 3.3, `memtx_tuple` up to 3.5), only what the build has; the list printers survive an error;
- an unknown MsgPack extension prints as `ext:<type> of len <n>`; no `[invalid]` after a UUID (any 128 bits are one since 3.2);
- a printer whose enum, variable or type the debug info lacks is off with a note, the rest loads.

```
(gdb) source tarantool-gdb.py
(gdb) info tt-fibers
(gdb) tt-fiber 105
(gdb) bt
(gdb) print *tuple
(gdb) tt-mp -pretty -- (char *)data
(gdb) tt-list -fields fid,name -- main_cord.alive
(gdb) print $tl_var_like(&main_cord)
```

| Kind | Names |
|---|---|
| commands | `info tt-fibers`, `tt-fiber`, `tt-mp`, `tt-list`, `tt-list-walk` |
| functions | `$tl_var_addr`, `$tl_var_find_offset`, `$tl_var_like`, `$find` |
| parameters | `print tt-list-entry`, `tl-var-search-range` |
| pretty-printers | `struct tuple`, `json_token`, `rlist`, `stailq`, `stailq_entry`, `decimal_t`, `struct tt_uuid`, `struct datetime`; MsgPack (`char *`) in `tt-mp` |

### Tests

The group `tarantool` (`tests/dbg/test_tarantool.py`) runs one instance (`box.cfg{}` with no listen, its files in a work directory under `TMPDIR`) that builds a space and a tuple, fibers (a sleeper, one with an open transaction) and MsgPack from tarantool's own encoders, and checks every command and printer live and on the `gcore` file of the same stop against what the Lua side knows (`fiber.info()`, the tuple, `tostring()` of decimals, UUIDs and datetimes, `box.txn_id()`) and what gdb reads on its own (list lengths, TLS addresses): fibers in every thread, the fiber switch and its frames, the list commands and printers, tuples from every thread, all MsgPack types and extensions, the thread-local functions, also with gdb's TLS lookup and with parts of the debug info hidden. A thread whose stack `gcore` could not read (a guard region, Linux 6.13+ with a recent glibc) is left out of the core checks with a note.

The group `upstream-tarantool` runs `test/gdb/` of PR #13078 and `tools/test-tarantool-gdb.py` of PR #9665 unchanged with pytest (`python3-pytest`, or `DBG_PYTHON`), their `conftest.py` replaced by `tests/dbg/upstream/tt_fixtures.py` (the core comes from `gcore`, no `core_pattern` needed). `DBG_TT_GDB=$LJBT_TT_GDB` runs them with the file of the release.

## License

BSD 2-Clause, see `LICENSE`, except for `luajit_dbg.py` and its vendored tests, which are MIT as LuaJIT, and `tarantool-gdb.py` and its vendored tests, which are BSD 2-Clause of the Tarantool authors: see `NOTICE` for where they come from.
