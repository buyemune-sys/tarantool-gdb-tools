//! A module on the `tarantool` crate, for the `rust` group of the lj-bt
//! tests (tests/test_rust.py, tests/lua/rust.lua).
//!
//! `require('ljbt_tnt')` sets the globals:
//!
//! * `ljbt_tnt_call(x)`: a tlua function; it calls the Lua global
//!   `ljbt_from_tlua(x)` in a new Lua thread (`tarantool::lua_state()`), as
//!   Rust code calling Lua does: a VM entry with no Lua frames below it in
//!   its `lua_State`.
//! * `ljbt_tnt_spawn_lua()`: starts the fiber "ljbt-rust-lua", which calls
//!   the Lua global `ljbt_fiber_body()` (it sleeps in `fiber.sleep`) in a new
//!   Lua thread; returns the fiber id.  The fiber has no Lua stack of its own
//!   (`fiber->storage.lua.stack` is NULL).
//! * `ljbt_tnt_spawn_rust()`: starts the fiber "ljbt-rust-only", which
//!   sleeps in Rust until `ljbt_tnt_stop()`; returns the fiber id.
//!
//! The stored procedure `ljbt_tnt.ljbt_proc(x)` (`box.schema.func.create`,
//! language C) calls the Lua global `ljbt_from_proc(x)` the same way as
//! `ljbt_tnt_call`.

use std::ffi::c_int;
use std::hint::black_box;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tarantool::fiber;
use tarantool::tlua::{self, LuaFunction};

static STOP: AtomicBool = AtomicBool::new(false);

/// `name(x)` of the Lua globals, in a new Lua thread; it returns a boolean.
#[inline(never)]
fn call_global(name: &str, x: i64) -> bool {
    let lua = tarantool::lua_state();
    let f: LuaFunction<_> = lua
        .get(name)
        .unwrap_or_else(|| panic!("no Lua global {name}"));
    let r: bool = f
        .call_with_args(x)
        .unwrap_or_else(|e| panic!("{name}: {e}"));
    black_box(r)
}

#[tarantool::proc]
fn ljbt_proc(x: i64) -> bool {
    call_global("ljbt_from_proc", x)
}

/// The body of "ljbt-rust-lua".
#[inline(never)]
fn lua_fiber() {
    let lua = tarantool::lua_state();
    let f: LuaFunction<_> = lua
        .get("ljbt_fiber_body")
        .expect("no Lua global ljbt_fiber_body");
    f.call::<()>()
        .unwrap_or_else(|e| panic!("ljbt_fiber_body: {e}"));
    black_box(());
}

#[inline(never)]
fn nap() {
    fiber::sleep(Duration::from_millis(10));
    black_box(());
}

/// The body of "ljbt-rust-only".
#[inline(never)]
fn rust_fiber() {
    while !STOP.load(Ordering::Relaxed) {
        nap();
    }
}

fn spawn(name: &str, f: fn()) -> u64 {
    fiber::Builder::new()
        .name(name)
        .func(f)
        .start_non_joinable()
        .expect("cannot start a fiber")
}

/// # Safety
///
/// Called by `require` with a valid `lua_State`.
#[no_mangle]
pub unsafe extern "C-unwind" fn luaopen_ljbt_tnt(l: *mut tlua::ffi::lua_State) -> c_int {
    let lua = tlua::StaticLua::from_static(l);
    lua.set(
        "ljbt_tnt_call",
        tlua::function1(|x: i64| -> bool { call_global("ljbt_from_tlua", x) }),
    );
    lua.set(
        "ljbt_tnt_spawn_lua",
        tlua::function0(|| -> u64 { spawn("ljbt-rust-lua", lua_fiber) }),
    );
    lua.set(
        "ljbt_tnt_spawn_rust",
        tlua::function0(|| -> u64 { spawn("ljbt-rust-only", rust_fiber) }),
    );
    lua.set(
        "ljbt_tnt_stop",
        tlua::function0(|| STOP.store(true, Ordering::Relaxed)),
    );
    0
}
