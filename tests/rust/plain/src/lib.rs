//! A Lua C module and FFI functions on the bare Lua C API, for the `rust`
//! group of the lj-bt tests (tests/test_rust.py, tests/lua/rust.lua).
//! No crates: the Lua and box symbols resolve against the tarantool binary
//! when it loads the library.
//!
//! Lua side, `package.loadlib(path, 'luaopen_ljbt_plain')()`:
//!
//! * `call(f, ...)`: `f(...)` through `lua_call` (unprotected: a Lua error
//!   unwinds through the Rust frames), returns what `f` returns.  Two Rust
//!   frames: `l_call` (the Lua C function) and `invoke`.
//! * `call_t(f, ...)`: the same, the Lua C function being a trait method
//!   (`<ljbt_plain::Call as ljbt_plain::LuaEntry>::entry`): a demangled
//!   name with spaces.
//!
//! FFI side: `int32_t ljbt_ffi_hot(int32_t x)` returns `x + 1`; for
//! `x >= 4000` it first looks up a space whose name is `40 + (x - 4000)`
//! bytes long, from `lookup` (the `box_space_id_by_name` breakpoint of the
//! tests).
//!
//! Every Rust function between Lua and Lua is `#[inline(never)]` and does
//! something after its call, so that no frame is inlined or tail-called
//! away and the C stack is the same in every build.

use std::ffi::{c_char, c_int};
use std::hint::black_box;

#[allow(non_camel_case_types)]
#[repr(C)]
pub struct lua_State {
    _opaque: [u8; 0],
}

type LuaCFunction = unsafe extern "C-unwind" fn(*mut lua_State) -> c_int;

const LUA_MULTRET: c_int = -1;

// Any Lua API function may raise a Lua error, which LuaJIT throws as a
// foreign exception through the C frames: "C-unwind".
extern "C-unwind" {
    fn lua_call(l: *mut lua_State, nargs: c_int, nresults: c_int);
    fn lua_gettop(l: *mut lua_State) -> c_int;
    fn lua_createtable(l: *mut lua_State, narr: c_int, nrec: c_int);
    fn lua_pushcclosure(l: *mut lua_State, f: LuaCFunction, n: c_int);
    fn lua_setfield(l: *mut lua_State, idx: c_int, k: *const c_char);
}

extern "C" {
    fn box_space_id_by_name(name: *const c_char, len: u32) -> u32;
}

/// Calls the function at the bottom of the stack with the values above it
/// as arguments, leaves all its results.
#[inline(never)]
unsafe fn invoke(l: *mut lua_State) -> c_int {
    lua_call(l, lua_gettop(l) - 1, LUA_MULTRET);
    black_box(lua_gettop(l))
}

/// `call(f, ...)`.  Not exported: its name comes from the symbol table
/// only, there is none in the strip build.
unsafe extern "C-unwind" fn l_call(l: *mut lua_State) -> c_int {
    let n = invoke(l);
    black_box(n)
}

/// What a framework that wraps Rust functions for Lua looks like.
trait LuaEntry {
    unsafe extern "C-unwind" fn entry(l: *mut lua_State) -> c_int;
}

struct Call;

impl LuaEntry for Call {
    /// `call_t(f, ...)`.  Not the same code as l_call: identical functions
    /// are merged into one symbol.
    unsafe extern "C-unwind" fn entry(l: *mut lua_State) -> c_int {
        let n = invoke(l);
        black_box(n + 1) - 1
    }
}

#[no_mangle]
pub unsafe extern "C-unwind" fn luaopen_ljbt_plain(l: *mut lua_State) -> c_int {
    lua_createtable(l, 0, 2);
    lua_pushcclosure(l, l_call, 0);
    lua_setfield(l, -2, c"call".as_ptr());
    lua_pushcclosure(l, <Call as LuaEntry>::entry, 0);
    lua_setfield(l, -2, c"call_t".as_ptr());
    1
}

#[inline(never)]
fn lookup(seq: i32) -> u32 {
    let name = [b'x'; 64];
    let len = 40 + seq.clamp(0, 24) as u32;
    // SAFETY: name is 64 bytes long, len is at most 64.
    let id = unsafe { box_space_id_by_name(name.as_ptr().cast(), len) };
    black_box(id)
}

/// Called through FFI from a hot loop: from a JIT trace.
#[no_mangle]
pub extern "C" fn ljbt_ffi_hot(x: i32) -> i32 {
    if x >= 4000 {
        black_box(lookup(x - 4000));
    }
    x + 1
}
