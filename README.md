# tarantool-gdb-tools

Debugger scripts for Tarantool 3.x and its LuaJIT, live and on core dumps: the LuaJIT and Tarantool extensions in one version for every Tarantool 3.x (instead of the files each release ships), a mixed Lua/C backtrace, and a `gcore` that keeps the thread stacks.

| File | What | Debuggers | Tarantool | Arch |
|---|---|---|---|---|
| [`luajit_gdb.py`](#luajit_gdbpy) | LuaJIT's extension: `lj-stack`, `lj-tv`, `lj-trace`, `lj-gctop`, ... | gdb, lldb | 3.0.2 - 3.9.0 | x86_64, aarch64 |
| [`tarantool_gdb.py`](#tarantool_gdbpy) | Tarantool's extension: `info tt-fibers`, `tt-fiber`, `tt-mp`, `tt-list`, pretty-printers | gdb | 3.0.2 - 3.9.0 | x86_64, aarch64 |
| [`luajit_backtrace_gdb.py`](#luajit_backtrace_gdbpy-lj-bt) | `lj-bt`: one stitched C + Lua backtrace, and the same for plain `bt` | gdb | 3.3.5 - 3.9.0 | x86_64, aarch64 |
| [`gcore_pages_gdb.py`](#gcore_pages_gdbpy) | `gcore-pages`: a `gcore` that keeps the thread stacks with glibc 2.42+ and gdb before 18.1 | gdb | 3.0.2 - 3.9.0 | x86_64, aarch64 |

The ranges are what CI runs, the last patch of every minor in them; [`extensions.json`](#the-manifest) says the same for programs. `examples/` has scenarios to try the scripts on, `tests/` the tests.

Everything needs DWARF in the Tarantool binary. The static deb of Tarantool's package repository carries it; the stripped static rpm that `tt binary install` puts does not, but the deb binary of the same version has the same build-id and serves as its symbol file (`symbol-file` or `file` in gdb, also for a core of the rpm build).

A self-built Tarantool works as well: a build with DWARF (`RelWithDebInfo` or `Debug`), with or without GC64, which is usually a dynamic PIE that gdb relocates when the program starts, whereas the official packages of 3.x are static non-PIE builds. The scripts keep no address from before the start or from another process, and the `source` workflow builds 3.9.0 that way for the tests, see [CI](#ci).

## Loading

### gdb: source

```
(gdb) source luajit_gdb.py
(gdb) source tarantool_gdb.py
(gdb) source luajit_backtrace_gdb.py
(gdb) source gcore_pages_gdb.py
```

Any of them, at any time: before `file` (`luajit_gdb.py` then waits for the binary), before `run`, on a core. `luajit_backtrace_gdb.py` comes after `luajit_gdb.py`, whose helpers it takes; `tarantool_gdb.py` is optional for it (fibers), before or after it.

### gdb: auto-load

gdb runs the scripts of a binary when it loads it: `<binary>-gdb.py` and `<binary>-gdb.gdb` under an auto-load directory, `/usr/share/gdb/auto-load` by default, followed by the real path of the binary. The files here have underscores in their names (for lldb, below), so the script is a link:

```
$ sudo mkdir -p /usr/share/gdb/auto-load/usr/bin
$ sudo ln -s "$PWD/tarantool_gdb.py" /usr/share/gdb/auto-load/usr/bin/tarantool-gdb.py
```

A binary has one `-gdb.py`; the other files go into its `-gdb.gdb`, which gdb runs too:

```
$ printf 'source %s/luajit_gdb.py\nsource %s/luajit_backtrace_gdb.py\n' "$PWD" "$PWD" |
    sudo tee /usr/share/gdb/auto-load/usr/bin/tarantool-gdb.gdb
```

`info auto-load` shows what was loaded. Without root, the same files go under a directory of your own, which `~/.config/gdb/gdbinit` names by its full path (gdb does not expand `~` there):

```
add-auto-load-scripts-directory /home/me/.local/share/gdb/auto-load
add-auto-load-safe-path /home/me/.local/share/gdb/auto-load
```

with the links at `/home/me/.local/share/gdb/auto-load/<real path of the binary>-gdb.py` and `-gdb.gdb`.

### lldb

Only `luajit_gdb.py` works in lldb:

```
(lldb) target create /usr/bin/tarantool
(lldb) command script import luajit_gdb.py
(lldb) process handle SIGALRM --pass false --stop false --notify false
```

lldb imports a script as a Python module named after the file and refuses a name with dots or dashes in it; that is why every file here has underscores. Import it after `target create`: there is no deferred loading in lldb. The `process handle` line is for a live process: Tarantool's low-resolution clock, a 10 ms `ITIMER_REAL`, keeps a `SIGALRM` pending in a stopped process, and lldb's `n` then stops where it began.

## luajit_gdb.py

LuaJIT's gdb and lldb extension in one version for every Tarantool 3.x, from 3.0.2 to 3.9.0, x86_64 and aarch64, instead of the one of each release: `luajit-gdb.py` with 7 commands up to 3.5, `luajit_dbg.py` with 15 from 3.6. It is `src/luajit_dbg.py` of tarantool/luajit at Mikhail Elhimov's branch `elhimov/gh-13094-dbg-avoid-hardcoded-enums`, which takes the enums from the inferior, with the changes below on top.

| Command | What |
|---|---|
| `lj-arch` | `LJ_64`, `LJ_GC64`, `LJ_DUALNUM` of the build |
| `lj-bc <BCIns *>` | one bytecode instruction and its operands |
| `lj-ctype <CType *>` | the ID and the name of a C type |
| `lj-func <GCfunc *>` | a function: its chunk, lines and bytecode |
| `lj-gc` | GC stats: state, total, threshold, debt, estimate, the object lists |
| `lj-gco <GCobj *>` | a GC object, its type and contents |
| `lj-gctop [<amount>]` | the number and total size of the GC objects, and the largest ones |
| `lj-ir <IRIns *>` | one IR instruction |
| `lj-jslots [<lua_State *>]` | the slots of the JIT stack map |
| `lj-proto <GCproto *>` | a prototype: its chunk, lines and bytecode |
| `lj-stack [<lua_State *>]` | the Lua stack of a coroutine, the main one by default |
| `lj-state` | the VM, GC and JIT states |
| `lj-str <GCstr *>` | a string: payload, size, hash |
| `lj-tab <GCtab *>` | a table: metatable, array and hash parts |
| `lj-trace [/FLAGS] <GCtrace *>` | a trace: its IR and start; `/r` the registers, `/s` the snapshots |
| `lj-tv <TValue *>` | a TValue, its type and value |

```
(gdb) lj-stack
(gdb) lj-gctop 20
```

For a user of the release files: every command on every 3.x (`lj-bc`, `lj-func`, `lj-trace` and the others appeared in 3.6), the enums from the binary, not hardcoded, and the changes on top of that branch, each a commit of its own:

- `lj-gctop [N]`: the number and total size of the GC objects and the N largest, ported from the branch `elhimov/gh-noticket-gdb-gctop`;
- the red zone of `lj-stack` is taken from the stack: it is 7 slots, not 8, in the LuaJIT of 3.0, 3.1 and 3.4;
- a Lua frame is `[L]`, never `[LP]` (that P was a bit of the return PC);
- the `lits` operand of `lj-bc` is signed (`KSHORT -7`, not `65529`);
- `lj_bc_mode` and `lj_ir_mode` are looked up on every use: the value kept from the first lookup had the address of another process after `core-file` of another core or an attach (a PIE moves);
- with no `lua_State` (before Tarantool sets `tarantool_L`, and since 3.7 in every thread with no Lua) the commands say so instead of a Python error;
- if the debug info lacks an enum (it may leave out one used only through its constants, tarantool/tarantool#13094), the built-in members are taken, the same in the LuaJIT of every 3.x, with a note;
- in lldb: `tarantool_L`, `__thread` since 3.7, is read in a frame (the target has no thread for it, and a frame of assembly or of a library without debug info has no scope), the memory of an expression is got around an lldb-server bug (below), the `.rodata` tables (`lj_bc_mode`, `lj_ir_mode`) are read from the executable on a core, and the commands are registered under the module's own name, whatever the file is called.

`lj-bt` works on top of it as on top of the extension of the release (the bytecode and IR tables are there for every version).

### In lldb

Every command works live and on a core (tested on x86_64 with lldb 22, live and on cores, and lldb 18, live), with these differences:

- lldb-server gets the memory of an expression with an `mmap` it runs in the inferior and takes the stop of any pending signal for its end (`NativeProcessLinux::Syscall`, LLVM main as well); the `SIGALRM` of Tarantool's clock is one. The extension allocates until lldb has good pages before its first expression; `p` and `expression` of your own fail the same way before that.
- the same `SIGALRM` makes `n` stop where it began: `process handle SIGALRM --pass false --stop false` before stepping.
- no deferred loading: import it after `target create`.
- lldb on Linux writes minidumps only (`process save-core`); it reads ELF cores (tried with gdb's `gcore`) as usual. lldb 18 reads no `__thread` variable from a minidump (it does from an ELF core) and fails to write some.

## tarantool_gdb.py

Tarantool's gdb extension in one version for every Tarantool 3.x, from 3.0.2 to 3.9.0, x86_64 and aarch64, instead of `tools/tarantool-gdb.py` of each release. It is that file of tarantool/tarantool at Mikhail Elhimov's PR #13078 (master and his fixes: `gdb.printing` for gdb 7.6, the architecture for gdb 8, a fiber switch without libthread_db), with his MsgPack refactor of PR #9665 on top, and the changes listed [below](#differences-from-the-tarantool-gdbpy-of-the-releases).

| Kind | Names |
|---|---|
| commands | `info tt-fibers [ID]...`, `tt-fiber [FIBER_ID]`, `tt-mp [OPTION... --] EXP`, `tt-list [OPTION... --] LIST_EXP`, `tt-list-walk [OPTION... --] LIST_EXP` |
| parameters | `print tt-list-entry`, `tl-var-search-range` |
| functions | `$tl_var_addr`, `$tl_var_find_offset`, `$tl_var_like`, `$find` |
| pretty-printers | `struct tuple`, `json_token`, `rlist`, `stailq`, `stailq_entry`, `decimal_t`, `struct tt_uuid`, `struct datetime`; MsgPack (`char *`) in `tt-mp` |

`help <command>` has the options and examples. On Tarantool 3.9.0, a sleeping fiber and a tuple (`break luaT_pushtuple` under `box.tuple.new({1, 'abc', {x = 2}})`):

```
(gdb) info tt-fibers
  Id     Target   Name                               Stack              Function
  105    TtFiber  "lua"                              0x7ffff6580e18     0x619ce0 <lua_fiber_run_f>
* 104    TtFiber  "main"                           * 0x7ffff6480d58     0x617260 <run_script_f>
  103    TtFiber  "on_shutdown"                      0x7ffff7380f98     0x438860 <on_shutdown_f(typedef __va_list_tag __va_list_tag *)>
  ...
(gdb) tt-fiber 105
Switching stack unwinder to fiber 105 (0x7ffff70009f0 (lua))
#1  0x000000000064571f in fiber_yield () at /tarantool/src/lib/core/fiber.c:895
(gdb) bt 3
#1  0x000000000064571f in fiber_yield () at /tarantool/src/lib/core/fiber.c:895
#2  fiber_yield_timeout (delay=<optimized out>) at /tarantool/src/lib/core/fiber.c:939
#3  0x0000000000645ac5 in fiber_sleep (delay=<optimized out>) at /tarantool/src/lib/core/fiber.c:967
(gdb) tt-fiber 104
(gdb) print *(struct tuple *)tuple
$1 = {local_refs = 1 '\001', flags = 0x00, format_id = 0, ..., data_offset = 6, data = {1U, "abc", {["x"] = 2U}}}
(gdb) tt-mp -pretty -- "\x93\x01\xa1x\x81\xa1y\x02"
$2 = {1U, "x", {
    ["y"] = 2U
  }}
(gdb) tt-list -fields fid,name -- main_cord.alive
$3 = rlist<fiber::link> of length 5, ref=*(rlist*)0x3338480 = {[0] ((fiber*)0x7ffff70009f0) = {fid = 105, name = 0x7ffff7000bb0 "lua"}, ...}
(gdb) print $tl_var_like(&main_cord)
$4 = (struct cord *) 0x3338420 <main_cord>
```

For a fiber that is not running, `bt` leaves out frame #0, and the inline frames down to the first one that is not (at a crash in glibc with its debug info, `__pthread_kill_implementation` and `__pthread_kill_internal`): their stack pointer is that of the running fiber. `tt-fiber` selects the first frame left.

Known limitations:

- the `struct tuple` printer takes a `struct tuple`, not its typedef `box_tuple_t` of the public API: `print *(struct tuple *)tuple` where `tuple` is a `box_tuple_t *` (`print *tuple` there says "expression doesn't evaluate to tuple"; so does the printer of the releases);
- after `tt-fiber` of a fiber that is not running, the thread pointer is not there (`$fs_base` is `<not saved>`) and the thread-local functions fail ("Failed to find the given value in any TLS"): `tt-fiber` of the running fiber first.

### Differences from the tarantool-gdb.py of the releases

- **`tt-tuple` is gone**: the `struct tuple` printer shows the tuple's data, so `print` is the command:
  ```
  (gdb) tt-tuple tuple                          # a release
  (gdb) print *tuple                            # here, tuple a struct tuple *
  $1 = {local_refs = 1 '\001', flags = 0x00, format_id = 0, ..., data_offset = 6, data = {1U, "abc", {["x"] = 2U}}}
  (gdb) print tuple->format_id                  # any field, as for any struct
  ```
- **The `struct interval` printer is gone.** In every release it raised `TypeError: Interval.__init__() takes 1 positional argument but 2 were given` at each value: it never read one. gdb prints the fields of the struct; MsgPack intervals (the `MP_INTERVAL` extension) are decoded by `tt-mp` and in tuples.
- **It loads with no process**, before `run` or on a core, and with no `python import gdb.printing` before it. The file of the releases fails without them: "module 'gdb' has no attribute 'printing'", then "Cannot read `cord_ptr' without registers" (up to 3.6) or "Cannot find address of TLS symbol `tuple_formats' without registers" (3.7+). That is what makes it fit for auto-load.
- `tt-mp` prints through the pretty-printers: any `print` option works and ends with `--` (`tt-mp -pretty -- EXP`; the old `-max-length N` is `-elements N`), MsgPack in a string literal can be printed (`tt-mp "\x92\x01\xa1x"`), and the output is gdb's: `{1U, "abc", {["x"] = 2U}}` where a release printed `[1U, "abc", {"x": 2U}]`.
- the thread-local functions `$tl_var_addr`, `$tl_var_find_offset`, `$tl_var_like` and `$find`, and the parameter `tl-var-search-range` (PR #13078, in no release). Thread-local variables gdb cannot read (a core with no libthread_db before gdb 17, whose internal TLS support reads the main thread's on a core) are read at their offset from the thread pointer: `$tl_var_find_offset` reads the TLS itself (gdb's memory search gives wrong addresses past unreadable memory, and crashed on cores), takes the match closest to the thread pointer, looks as far as the TLS segment goes (`cord_ptr` is 197 KiB away in 3.7 and 3.8), above `$tpidr` on AArch64, and prints no debug lines. Where gdb has no register for the thread pointer (gdb 12 on AArch64: `$tpidr` came in gdb 13) it is taken from the thread handle of libthread_db, the address of glibc's `struct pthread`, plus the size of that struct (`_thread_db_const_thread_area`).
- the fiber switch: the unwinder tells a thread by its ptid (gdb aborted on `thread N` after `info tt-fibers` on a core) and gives the saved context of the selected fiber to the innermost frame it is asked about, told by its stack pointer and pc, learned again when the thread runs (the selection did not survive a flush of the frame cache; given to frame level 0 only, it missed a crash under inline frames, #36); `tt-fiber` selects the first frame of the fiber, past those inline frames; what `lj-bt` uses stays: `fiber()`, `Cord().fibers()`, `FiberUnwinder.instance().fiber()`, `set_fiber()`, `reset_fiber()`.
- since 3.7 `tuple_formats` is thread-local: it is read when a tuple is printed, from the main thread where the current one has none, so tuples print from every thread.
- the printers of `decimal_t`, `struct tt_uuid` and `struct datetime` print the value (a Python traceback in every release), with the 3-digit units of decNumber; MsgPack decimals have their 76 digits since 3.5.
- the list table has the lists of the older releases (quorum gain/loss up to 3.5, `all_txs` and the vinyl writers up to 3.3, `memtx_tuple` up to 3.5), only what the build has; the list printers survive an error.
- an unknown MsgPack extension prints as `ext:<type> of len <n>`; no `[invalid]` after a UUID (any 128 bits are one since 3.2).
- a printer whose enum, variable or type the debug info lacks is off with a note (`tarantool_gdb.py: no value MP_ERROR_MAX in the debug info, the MsgPack error printer is off`), the rest loads; a release fails as a whole.
- the datetime, MsgPack error and compression printers look their arrays (`zones_unsorted`, `mp_error_field_to_json_key`, `compression_type_strs`) up on use: kept from the load, they had the link-time address of a PIE sourced before `run`; a `tt-list-walk` belongs to one process and starts over in another (on a core of another process it kept walking the old items).

## luajit_backtrace_gdb.py (lj-bt)

`lj-bt` prints the C frames and the Lua frames of a Tarantool process in the real order of the calls. Plain `bt` shows only the C stack, and `lj-stack` shows only the Lua stack of one coroutine; matching them by eye is what this replaces. Loading it also installs a frame filter, so plain `bt` prints the stitched stack, and an unwinder, so `bt` goes through JIT traces.

CI runs it on the official static debs of Tarantool CE 3.3 to 3.9, x86_64 and aarch64, after `luajit_gdb.py` and `tarantool_gdb.py`, and its oracle, core and cli layers once more after the extensions of each release (see [CI](#ci)). It has run with Tarantool master built from source with DWARF, with and without GC64; 3.0.2 passes the oracle, core and cli layers too, outside CI.

### Usage

```
(gdb) source luajit_gdb.py
(gdb) source luajit_backtrace_gdb.py
(gdb) source tarantool_gdb.py            # optional: fibers
(gdb) lj-bt [-v] [-n N] [--tail-chain] [--no-tail] [<lua_State *>]
(gdb) lj-bt --all-fibers
```

The LuaJIT extension may also be the one of the LuaJIT the binary was built with: `src/luajit_dbg.py` of tarantool/luajit at the commit of Tarantool's `third_party/luajit`, which is `src/luajit-gdb.py` before LuaJIT 5db8c217 (Tarantool 3.3-3.5); `tests/fetch.sh` gets the files of a release. `lj-bt` takes only the helpers all of them have; the bytecode and IR names it needs come from the DWARF of the binary (if a build has no such enum, from the tables of `luajit_gdb.py` or `luajit_dbg.py`, with a `--` note; `luajit-gdb.py` has none, then `lj-bt` says so and stops).

For fibers, load `tarantool_gdb.py` too (or `tools/tarantool-gdb.py` of the release, after the process has started and `python import gdb.printing`). After `tt-fiber <id>`, `lj-bt` prints the stack of that fiber; `--all-fibers` prints every fiber of the current cord.

- `bt -no-filters` is the raw gdb backtrace; `disable frame-filter global lj-bt` turns the filter off.
- C frames keep gdb's numbers; the Lua frames of a VM band are `#N.1`, `#N.2`, ...
- Lines marked `!!` are mismatches or heuristics, never silent guesses.

`examples/example.gdb` and `examples/mixed.lua` reproduce the cases below: `tarantool examples/mixed.lua <sort|pcall|coro|error|trace|sleep|tail> <on|off> <main|fiber>`; from the repository root:

```
gdb -x examples/example.gdb --args tarantool examples/mixed.lua sort on main
```

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

A C function called from a JIT trace (`mixed.lua trace on main`, `break box_space_id_by_name if len == 8`).  gdb alone stops at `??` there; with `luajit_backtrace_gdb.py` loaded even `bt -no-filters` goes on through the trace's band.

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

### Machine-readable output

`lj-bt --json [-o FILE]` prints the same backtrace as one JSON document: the frames with their Lua entries, the C function folded into each, the `!!` mismatches and the `--` notes, and per `lua_State` how many frame links were walked and hidden. The tests compare it rather than the text.

### Known limitations

- In a trace, the frames are those of the snapshot that covers the pc, the one `lj_trace_unwind` would restore: the line may lag a line behind, and the function may be one that later tail-called further.  Inlined frames that no snapshot of the trace holds are not shown: LuaJIT takes a snapshot only where a guard needs one, and around a call from a trace the covering snapshot is the one after the call (`test_core.test_hop_snapshot_frames`: a loop calls two Lua functions inlined into the trace, the second makes an FFI call, and only the loop's frame is there).
- arm64 is checked by CI only. Its layouts come from `lj_frame.h` and `vm_arm64.dasc`: the VM's CFI there is based on the frame pointer, and `BC_JLOOP` lowers sp by 16 as on x64, which the trace unwinder takes into account. arm64 traces keep no trace number in `vmstate`, so the header says just `TRACE`. The register top of the stack (BASE/PC of the VM, x19/x21 there) is read on arm64 as well, untried outside CI.
- gdb stops at the bottom of a fiber stack with "previous frame identical" on arm64 (`coro_startup`, and `coro_transfer` with a pc 0 below for a fiber that has not run yet); that is a `--` note, not a mismatch.
- Lua tail calls are recovered by the heuristic above only; a tail call into C shows as a missing C frame.
- `tarantool_L` is thread-local since 3.7: where gdb reads no thread-local variable, `lj-bt` has no G. The header says `L=0x0 (none)`, the Lua frames come from the VM entries, and the tail call heuristic does not follow `__index`.
  - gdb 17 and later read thread-local variables without `libthread_db`: "GDB-internal Thread Local Storage (TLS) support" in the gdb 17 section of gdb's NEWS, for x86_64, aarch64 and others. gdb 16.3 does not.
  - gdb before 17 needs `libthread_db`, and on a core it often has none ("thread debugging will not be available"). That happens when no `libthread_db` comes with the core's libraries, and when the `libthread_db.so.1` found is outside `auto-load safe-path` (add its directory with `add-auto-load-safe-path`).
  - It also happens when gdb cannot load that `libthread_db` into its own process. Its version needs (`readelf -V`) must be met by the libc gdb runs on. The x86_64 `libthread_db` of glibc 2.42-2.44 needs `GLIBC_ABI_DT_X86_64_PLT`, which the glibc 2.39 of Ubuntu 24.04 lacks ("Unable to find libthread_db matching inferior's thread library").
  - Measured on x86_64 with a core of a PIE master build: gdb 15.1 on glibc 2.39 cannot read `tarantool_L` with the `libthread_db` of glibc 2.43, whether the binary comes by `file` or by `symbol-file`; gdb 17.1 reads it.
  - On AArch64, gdb 12 has no thread-pointer register (`$tpidr` came in gdb 13). With `libthread_db` it still reads thread-local variables: the lj-bt `core` layer and the thread-local tests of `tarantool_gdb.py` pass on `ubuntu-22.04-arm` in `cores.yml`.
- gdb's inline frames of `-O2` C code are printed as `[inlined]` and not otherwise examined.
- `tarantool_L` is shared by several fibers whose cframes lie on different C stacks; without `tarantool_gdb.py` such frames are marked, not attributed to a fiber.
- Not tried: hooks (the parser of `load()` under `lj_vm_cpcall` without a frame, FFI callbacks and the error handler of `xpcall` are steps of the test generator).

## gcore_pages_gdb.py

A core that gdb's `gcore` writes of a running Tarantool can hold the stack of the main thread only. Every other thread (`wal`, `iproto`, `applier_1`, `coio`...) stops after one frame, its thread-local variables read as zeroes, and `gcore` warns once per lost mapping:

```
warning: Memory read failed for corefile section, 1048576 bytes at 0x7f81343ff000.
...
(gdb) thread apply all bt          # on that core
Thread 4 (LWP 232073):
#0  __syscall_cancel_arch () at ../sysdeps/unix/sysv/linux/x86_64/syscall_cancel.S:56
#1  0x0000000000000000 in ?? ()
```

Why: since glibc 2.42 (commit a6fbe36b7f) `pthread_create` installs the guard page of a thread stack with `madvise(MADV_GUARD_INSTALL)` (Linux 6.13+) inside the stack mapping, at its start, instead of a `PROT_NONE` mapping of its own; several stacks often merge into one mapping. A read of a guard page fails (`EIO` from `/proc/PID/mem`). `gcore` before gdb 18.1 copies each mapping in reads of 1 MiB and gives up on the rest of the mapping at the first read that fails, so the stacks, with the TLS and `struct pthread` at their top, are zeroes in the core. This is gdb [bug 33855](https://sourceware.org/bugzilla/show_bug.cgi?id=33855), fixed in gdb 18.1 (commit c1da013915 retries such a read page by page).

Who has it: glibc 2.42+ on Linux 6.13+ with a gdb before 18.1 (all three). Seen on Ubuntu 26.04 (glibc 2.43, Linux 7.0, gdb 17.1); Ubuntu 25.10 (glibc 2.42, gdb 16.3) and 26.10 (glibc 2.44, gdb 17.2) ship the same mix. Not on Ubuntu 24.04 (glibc 2.39, even on Linux 7.0) or 22.04, nor with gdb 18.1+. The `cores` workflow checks each Ubuntu runner, see [CI](#ci). The kernel's own core (a crash, or `kill -QUIT` with `ulimit -c` and a file `core_pattern`) keeps the stacks: it leaves out the guard pages only.

What to do, any of:

- `gcore-pages` of `gcore_pages_gdb.py` instead of `gcore`; it runs `gcore FILE`, then reads every mapping of the core again as gdb 18.1 does and writes into `FILE` what `gcore` left out (the guard pages stay zeroes). The process is not changed; with gdb 18.1+ it is plain `gcore`. On a Tarantool with a core of 830 MB it took 1.0 s, `gcore` 0.8 s.
  ```
  (gdb) source gcore_pages_gdb.py
  (gdb) gcore-pages core.12345
  Saved corefile core.12345
  gcore-pages: read again 41963520 bytes of 5 mappings, 6 pages unreadable

  $ gdb -p PID -batch -x gcore_pages_gdb.py -ex "gcore-pages core.PID"    # instead of gcore -o core PID
  ```
  Where `gcore` lost nothing it says `gcore-pages: gcore read every mapping`.
- gdb 18.1 or later;
- the kernel's core, where the process may die.

What does not help (measured on Ubuntu 26.04): `set use-coredump-filter off`, `set dump-excluded-mappings on`; `GLIBC_TUNABLES`: glibc has no tunable for this guard (it takes a `PROT_NONE` mapping only where the `madvise` fails), and `glibc.mem.decorate_maps=1`, `glibc.pthread.stack_hugetlb=0` or `glibc.pthread.stack_cache_size=0` change nothing.

## The manifest

`extensions.json` lists the four files for the programs that load them (tt's `coredump` module):

```
{"schema": 1, "extensions": [
  {"file": "luajit_gdb.py", "debuggers": ["gdb", "lldb"], "after": [],
   "tarantool": {"min": "3.0.2", "max": "3.9.0"}, "arch": ["x86_64", "aarch64"],
   "commands": ["lj-arch", "lj-bc", ...], "parameters": []},
  ...]}
```

| Field | What |
|---|---|
| `file` | the file, in the repository root |
| `debuggers` | `gdb`, `lldb`: where CI loads it and runs its tests |
| `after` | files of the manifest to load before it, in the same debugger |
| `tarantool` | `min` and `max`: every minor between them passed CI at its last patch, on each `arch` |
| `arch` | `x86_64`, `aarch64` (as `uname -m` says them) |
| `commands` | what it registers, by the full name as typed (`info tt-fibers`, `lj-bt`), the same in each of its debuggers |
| `parameters` | optional: its `set`/`show` names (`print tt-list-entry`) |

Pretty-printers, unwinders, frame filters and convenience functions are in neither list.

`tests/manifest/check.py` checks it. `static` (`.github/workflows/manifest.yml`): the schema, the files, an `after` order that can be satisfied, and that the matrix of the workflow that runs each file in each debugger covers its range and arches and runs the next check. `gdb` and `lldb` (in the jobs of `test.yml`, `dbg.yml`, `cores.yml` and `lldb.yml`, with the binary of `$LJBT_TARANTOOL`): each file is loaded after its `after`, and the commands and parameters it added, read back from the debugger (`help all`, `command script list`), must be those of the manifest, exactly. `tests/manifest/test_check.py` shows that a wrong manifest fails.

```
python3 tests/manifest/check.py static
LJBT_TARANTOOL=<tarantool> python3 tests/manifest/check.py gdb
python3 tests/manifest/check.py --manifest wrong.json static
```

## Tests

### lj-bt

`tests/run.sh [GROUP...]` runs the tests of `lj-bt` against a Tarantool build with DWARF, after `luajit_gdb.py` and `tarantool_gdb.py` of the repository (`LJBT_LUAJIT_DBG` and `LJBT_TT_GDB` take others, an empty `LJBT_TT_GDB` none):

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
| `window` | lj-bt at every instruction of the VM between a mark and the target of a chain, through one step of each kind (`stepi`; C functions it calls run by `finish`): the C frames kept, every mismatch marked, else the Lua frames a stack the chain goes through, and none lost on the way down; and the stops of `stops` with `SIGPROF` from `setitimer` instead of sysprof, also stepped into the signal's handler (runs on arm64) |
| `all` | everything; the default is all but `crash` |

`tests/fetch.sh <version> <amd64|arm64> <dest>` downloads the official static deb of a release, unpacks it, adds the gdb extensions of its sources and prints `LJBT_TARANTOOL`, `LJBT_RELEASE_LUAJIT_DBG` and `LJBT_RELEASE_TT_GDB`, so a release is tested with:

```
export $(tests/fetch.sh 3.9.0 amd64 ~/tarantool-3.9.0)
LJBT_SEEDS=200 LJBT_STOPS=500 tests/run.sh all
LJBT_LUAJIT_DBG=$LJBT_RELEASE_LUAJIT_DBG LJBT_TT_GDB=$LJBT_RELEASE_TT_GDB tests/run.sh oracle core cli
```

the last line with the extensions of the release. A failed seed is replayed with `LJBT_SEED0=<n> LJBT_SEEDS=1 tests/run.sh random`. Temporary files go to `TMPDIR`; the box scenarios listen on unix sockets only. `stops` and `crash` need sysprof and skip themselves without it (arm64); `window` does not. Breakpoint conditions on Lua numbers go through `$ljbt_arg(L, n)` of `tests/probe.py`: arm64 LuaJIT is dual-number, an integer there is an int32 with a tag, and `->n` would read its bits as a double. `LJBT_LJ_BT` runs the tests against another `luajit_backtrace_gdb.py`, to see a new test fail before its fix.

#### The rust group

`tests/rust` holds two Rust modules, each a cargo workspace of its own with its `Cargo.lock`. `tests/rust/build.sh` builds them into `tests/rust/target` (or `LJBT_RUST_TARGET`); the test runs it too, cargo keeps it incremental.

- `plain`: a Lua C module and an FFI function on the bare Lua C API, no crates, so it builds offline. Four profiles: `ljbt-debug` (release with debuginfo), `release` (symbols only), `ljbt-strip` (no symbols, the Rust frames are `??`), `ljbt-abort` (`panic = "abort"`).
- `tnt`: on the `tarantool` crate 11.0.0 (rustc 1.89 or newer): `tlua` calls into Lua, fibers started from Rust, a C stored procedure (`#[tarantool::proc]`).

`tests/lua/rust.lua` runs the scenarios: Lua -> Rust -> `lua_call` -> Lua -> Rust -> Lua -> a box C function; 30 levels of Lua <-> Rust recursion; a Lua error thrown through the Rust frames to a `pcall` (with `panic = "abort"` it aborts the process: the landing pad of the Rust frame calls `panic_cannot_unwind`); a Rust function called through FFI from a hot loop, in a JIT trace; a `tlua` function that calls Lua in a new Lua thread; the stored procedure through `box.func` and through an iproto `call`; a fiber started from Rust that sleeps in Lua and one that sleeps in Rust. The Lua frames are compared with `debug.getinfo`, the C frames with `bt -no-filters`, the VM bands and the Rust frames between them with the structure of the chain. The stops of the debuginfo build and of `tnt` are also written with `gcore`, and the cores must give the same output. Known errors of `lj-bt` are `expectedFailure` tests, the reason in a comment.

`box.func` loads a C module from a copy that it deletes right after `dlopen`: gdb on a core finds no file for it and has no symbols of its frames, unless `set solib-search-path` names a directory with the module under the name of the copy, `<package>.so`.

### luajit_gdb.py, tarantool_gdb.py, gcore_pages_gdb.py

`tests/dbg/run.sh [GROUP...]` runs the tests of `luajit_gdb.py` and `tarantool_gdb.py` against a Tarantool build with DWARF:

| Group | What |
|---|---|
| `luajit` | every command, live and on the `gcore` file of the same stop; the outputs must match and agree with an oracle: `debug.getinfo` for functions and frames, `jit.bc` for the bytecode, `jit.dump` for the IR, registers and snapshots of a trace, `collectgarbage('count')` for `lj-gctop`, gdb expressions for addresses, sizes and GC fields (default) |
| `upstream` | LuaJIT's own debugger tests, vendored unchanged in `tests/dbg/upstream/` (see its README), run against tarantool; what cannot pass there is skipped with the reason |
| `tarantool` | `tarantool_gdb.py`: every command and printer, live and on a core |
| `upstream-tarantool` | Tarantool's own tests of the extension, vendored unchanged (see `tests/dbg/upstream/README.md`), with pytest |
| `cores` | the stacks of the threads of tarantool in a `gcore` core, a `gcore-pages` core and the kernel's core of the same stop |
| `all` | all five |

```
export $(tests/fetch.sh 3.9.0 amd64 ~/tarantool-3.9.0)
tests/dbg/run.sh all
DBG_LUAJIT_DBG=$LJBT_RELEASE_LUAJIT_DBG tests/dbg/run.sh upstream     # the extension of the release
```

`DBG_LUAJIT_DBG` and `DBG_TT_GDB` point the tests at other extensions, to see a test of a fix fail before it. The cores are written with `gcore-pages`; `DBG_GCORE_PAGES=plain` writes them with gdb's `gcore`, which loses the thread stacks where `gcore-pages` is needed. `DBG_KEEP=1` keeps the work directories in `TMPDIR` (the plan, the result and the gdb log of every run). No scenario of `luajit_gdb.py` calls `box.cfg` or listens on a port.

`DBG_DEBUGGER=lldb` (and `DBG_LLDB=lldb-22`, say) runs the groups `luajit` and `upstream` in lldb: `tests/dbg/driver_lldb.py` runs the same plans, translating their gdb, and writes the cores as lldb's minidumps (modified memory, no `.rodata` as in a kernel core); the upstream tests run in their own lldb mode. gdb is still needed.

The live run sources the extensions before `run`, when gdb has not relocated a PIE yet, the core run after `core-file`. `TestOtherCore` (`tests/dbg/othercore.py`) runs a scenario twice with ASLR on and then its commands on the core of the second run in a gdb session that looked at the core of the first: each output must be the one of a fresh session. On a PIE that catches an address kept from the other process, on any build state kept from it.

`tests/build-tarantool.sh <version> <gc64|nogc64> <dest>` builds a tag from source instead of fetching a deb (a dynamic PIE with DWARF, `LUAJIT_ENABLE_GC64` on or off; the build dependencies are listed in the script) and prints the same variables as `tests/fetch.sh`, the extensions of that source tree as the release ones.

The group `tarantool` (`tests/dbg/test_tarantool.py`) runs one instance (`box.cfg{}` with no listen, its files in a work directory under `TMPDIR`) that builds a space and a tuple, fibers (a sleeper, one with an open transaction) and MsgPack from tarantool's own encoders, and checks every command and printer live and on the `gcore` file of the same stop against what the Lua side knows (`fiber.info()`, the tuple, `tostring()` of decimals, UUIDs and datetimes, `box.txn_id()`) and what gdb reads on its own (list lengths, TLS addresses): fibers in every thread, the fiber switch and its frames, the list commands and printers, tuples from every thread, all MsgPack types and extensions, the thread-local functions, also with gdb's TLS lookup and with parts of the debug info hidden. The core is written with `gcore-pages`, and the output of every thread on it must be the live one: with plain `gcore` (`DBG_GCORE_PAGES=plain`) the thread checks fail where glibc 2.42+ meets a gdb before 18.1.

The same group checks `tt-fiber` and `bt` with inline frames at the top (#36), with two waiting fibers, live, on the `gcore` file of the stop and on the kernel's core of another live run to it. The oracle of each fiber's stack is gdb's own `bt` of it at its `fiber_yield`, while it ran (tarantool keeps no C backtrace of a fiber on AArch64). `TestCrash*` crash tarantool (an FFI NULL write; its crash handler calls `abort()`): with the debug info of glibc 2.34+ (`libc6-dbg`) the top frames are inline ones on x86_64; on AArch64 glibc does not inline them, which the tests assert and name (`glibc-inline-top`). `TestInline*` stop the main fiber in `fiber.sleep()` at `fiber_yield` inlined into `fiber_yield_timeout`, from tarantool's own DWARF, on both arches. `test_inline_top` checks the inline top and says why it does not hold. Where a precondition does not hold the test is skipped with the reason, unless `DBG_REQUIRE` lists it: `inline-top` (inline frames at the top), `kernel-core` (the kernel writes a core file); then it fails. `dbg.yml` and `cores.yml` require both.

The group `upstream-tarantool` runs `test/gdb/` of PR #13078 and `tools/test-tarantool-gdb.py` of PR #9665 unchanged with pytest (`python3-pytest`, or `DBG_PYTHON`), their `conftest.py` replaced by `tests/dbg/upstream/tt_fixtures.py` (the core comes from `gcore`, no `core_pattern` needed). `DBG_TT_GDB=$LJBT_RELEASE_TT_GDB` runs them with the file of the release.

The group `cores` (`tests/dbg/test_cores.py`) runs tarantool with `box.cfg` (the `wal`, `iproto`, `applier` and `coio` threads) to a stop and takes three cores there: `gcore`, `gcore-pages` and the kernel's (`signal SIGQUIT`). A stack is kept if the 1 KiB above `$sp` and the backtrace are the live ones (in the kernel's core, taken after the threads ran again: if the 1 KiB is not zeroes and the backtrace goes down to the live outermost frame).

| Test | What |
|---|---|
| `test_guard_pages` | the cause: the stack mappings of the threads have unreadable pages where glibc 2.42+ runs on Linux 6.13+, and nowhere else |
| `test_gcore` | `gcore` keeps every stack; an expected failure where the stacks have such pages and gdb is older than 18.1, with the reason on stderr (`known-broken: ...`) |
| `test_gcore_pages` | `gcore-pages` keeps every stack, and reads pages again only where `gcore` lost them |
| `test_kernel_core` | the kernel's core keeps every stack; skipped with the reason where the kernel writes none (`core_pattern` pipes to a program, or the hard `RLIMIT_CORE` is 0) |

`DBG_CORES_SUMMARY=FILE` writes the facts and the results as JSON; `tests/dbg/cores_table.py` makes a Markdown table of such files. `DBG_GDB` runs the tests with another gdb, e.g. one built from source.

### The manifest

`cd tests/manifest && python3 -m unittest -v test_check`: the checks of [the manifest](#the-manifest) on wrong manifests and workflows; with `LJBT_TARANTOOL` set, also loaded into gdb.

## CI

| Workflow | Tarantool | Arch | What |
|---|---|---|---|
| `test.yml` | 3.3.5, 3.4.2, 3.5.2, 3.6.4, 3.7.2, 3.8.2, 3.9.0 | amd64, arm64 | lj-bt: every group after our extensions; oracle, core and cli after the extensions of the release; the manifest in gdb |
| `dbg.yml` | 3.0.2, 3.1.2, 3.2.4 and the above | amd64, arm64 | `tests/dbg` groups `luajit`, `upstream`, `tarantool` (with `libc6-dbg` and the kernel's cores, both required), `upstream-tarantool`; the upstream ones once more with the extensions of the release, for the step summary; the manifest in gdb and its tests |
| `lldb.yml` | 3.0.2 - 3.9.0 | amd64, arm64 | `upstream` with lldb 18 and 22, `luajit` with lldb 22; the manifest in lldb |
| `cores.yml` | 3.9.0 | `ubuntu-22.04`, `-24.04`, `-26.04`, amd64 and `-arm` | the group `cores` with the kernel's cores enabled (`sysctl kernel.core_pattern=core.%p`), then, with `libc6-dbg`, `luajit`, `tarantool`, the lj-bt `core` layer and the manifest with the gdb of the runner (12.1, 15.1, 17.1); the `table` job puts a row per runner in the step summary: OS, kernel, glibc, gdb, guard pages, whether `gcore`, `gcore-pages` and the kernel's core keep the stacks |
| `manifest.yml` | - | - | the static checks of `extensions.json` and the tests of the checks |
| `source.yml` | 3.9.0 built from source, GC64 and `LUAJIT_ENABLE_GC64=OFF` | amd64 | a dynamic PIE with DWARF from `tests/build-tarantool.sh` (cached under a key with the hash of the script, saved right after the build): every group of `tests/dbg`, and the lj-bt groups oracle, core, random, tail, cli, env and window, after our extensions |

- The binaries are the static debs of Tarantool's package repository (they carry DWARF), cached per version and architecture and shared by the workflows; the extensions of the release come from the tag (`tools/tarantool-gdb.py`) and from tarantool/luajit at the tag's `third_party/luajit` (`luajit-gdb.py` up to 3.5, `luajit_dbg.py` from 3.6).
- On push and pull requests the long lj-bt groups are cut: 50 seeds, 100 stops, two crash attempts of 20 s. The daily scheduled run takes 200 seeds, 500 stops, three attempts of 40 s.
- On arm64 LuaJIT has no sysprof (nor memprof), so `stops` and `crash` are skipped, with a notice in the log; the random stops of `window` come from `setitimer` and run there.
- `continue-on-error` for now: the arm64 jobs of `test.yml` and `dbg.yml`, every job of `lldb.yml` (its arm64 ones never ran before) and of `cores.yml`, until they are green.
- The Rust modules of the `rust` group are built with the runner's Rust (stable is installed if it is missing or older than 1.89), cached per architecture together with the crates.
- A failed job uploads its work directories (`gdb.log`, `probe.jsonl`, `violations.jsonl` of lj-bt; the plans, results and gdb logs of `tests/dbg`) as an artifact.
- When each `continue-on-error` goes: [`docs/ci-continue-on-error.md`](docs/ci-continue-on-error.md).

## Upstream bugs

Bugs found here in other projects, each with a reproducer in `docs/repro/` and the report text ready to file:

| Page | Bug | Fixed upstream |
|---|---|---|
| [`docs/upstream-gdb-gcore-guard-pages.md`](docs/upstream-gdb-gcore-guard-pages.md) | gdb's `gcore` loses the thread stacks with glibc 2.42+ on Linux 6.13+ | gdb 18.1; Ubuntu 25.10–26.10 lack it |
| [`docs/upstream-lldb-pending-signal.md`](docs/upstream-lldb-pending-signal.md) | lldb cannot allocate memory in a process with a pending signal (lldb-server, Linux) | no, still in LLVM main |

## License

BSD 2-Clause, see `LICENSE`, except for `luajit_gdb.py` and its vendored tests, which are MIT as LuaJIT, and `tarantool_gdb.py` and its vendored tests, which are BSD 2-Clause of the Tarantool authors: see `NOTICE` for where they come from.
