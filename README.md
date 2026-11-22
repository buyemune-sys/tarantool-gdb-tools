# tarantool-gdb-tools

gdb extensions for debugging Tarantool and its LuaJIT, live and on core dumps, on top of the extensions Tarantool and LuaJIT ship: `luajit_dbg.py` of LuaJIT (`lj-stack`, `lj-tv`, ...) and `tools/tarantool-gdb.py` of Tarantool (`info tt-fibers`, `tt-fiber`, ...). Each tool loads after them and reuses their helpers.

| Path | What |
|---|---|
| `lj_bt.py` | `lj-bt`: one stitched C + Lua backtrace, and the same for plain `bt` |
| `examples/` | scenarios and gdb scripts to try the tools on |
| `tests/` | automated tests, run against a Tarantool build with DWARF |

Status: prototypes. Everything needs DWARF in the Tarantool binary, as `luajit_dbg.py` does: official packages without debug info are not enough.

## lj-bt

`lj-bt` prints the C frames and the Lua frames of a Tarantool process in the real order of the calls. Plain `bt` shows only the C stack, and `lj-stack` shows only the Lua stack of one coroutine; matching them by eye is what this replaces. Loading it also installs a frame filter, so plain `bt` prints the stitched stack, and an unwinder, so `bt` goes through JIT traces.

Tested on x86_64 with Tarantool 3.10.0-entrypoint built from source with DWARF, LuaJIT 8af934d9 (the one of Tarantool 3.9.0).

### Usage

```
(gdb) source <luajit-src>/src/luajit_dbg.py
(gdb) source lj_bt.py
(gdb) lj-bt [-v] [-n N] [--tail-chain] [--no-tail] [<lua_State *>]
(gdb) lj-bt --all-fibers
```

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

- GC64 (FR2), which official builds use, is being tested now; until then, trace snapshots are not decoded under FR2, and BASE/PC are read from registers on x64 without GC64 only.
- In a trace, the line is that of the last snapshot before the pc and may lag a line behind.
- Lua tail calls are recovered by the heuristic above only; a tail call into C shows as a missing C frame.
- gdb's inline frames of `-O2` C code are printed as `[inlined]` and not otherwise examined.
- `tarantool_L` is shared by several fibers whose cframes lie on different C stacks; without `tarantool-gdb.py` such frames are marked, not attributed to a fiber.
- Not tried: ffi callbacks, hooks, the error handler of `xpcall`, `lua_cpcall` without a frame.

## License

BSD 2-Clause, see `LICENSE`.
