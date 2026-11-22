-- Scenarios of the `rust` group (tests/test_rust.py): Lua and the Rust
-- modules of tests/rust calling each other.
--
-- Environment: LJBT_WORKDIR (chain.lua), LJBT_LUA_DIR, LJBT_RUST_PLAIN
-- (libljbt_plain.so of one build), LJBT_RUST_TNT (the directory of
-- libljbt_tnt.so, for the tnt scenarios), LJBT_RUST_SCENARIOS (names,
-- comma-separated, run in this order), LJBT_JIT_MODE (on or off).
--
-- The stops are those of chain.lua: box.session.exists(4000 + seq)
-- (lbox_session_exists) and box_space_id_by_name with len 40 + seq.
-- Right before a target the scenario writes the oracle (H.oracle, from
-- debug.getinfo) on the same line.  The stop of 'error' is lj_err_throw
-- of the error 'ljbt-boom'.  Every scenario that gets to its end appends
-- a line to results.jsonl.
local H = dofile(os.getenv('LJBT_LUA_DIR') .. '/chain.lua')
local ffi = require('ffi')
local fiber = require('fiber')
local json = require('json')

local workdir = os.getenv('LJBT_WORKDIR')
local plain_path = os.getenv('LJBT_RUST_PLAIN')
local tnt_dir = os.getenv('LJBT_RUST_TNT')
local scenarios = os.getenv('LJBT_RUST_SCENARIOS') or ''

H.setup(os.getenv('LJBT_JIT_MODE') ~= 'off')

local function result(rec)
  local fh = assert(io.open(workdir .. '/results.jsonl', 'a'))
  fh:write(json.encode(rec) .. '\n')
  fh:close()
end

local R, lib
if plain_path then
  R = assert(package.loadlib(plain_path, 'luaopen_ljbt_plain'))()
  ffi.cdef('int32_t ljbt_ffi_hot(int32_t x);')
  lib = ffi.load(plain_path)
end

local function target(x)
  H.oracle(x - 4000) local r = box.session.exists(x) return r
end

-- basic (seq 1): Lua -> call (l_call) -> Lua -> call_t (a trait method)
-- -> Lua -> the target.
function _G.ljbt_basic_inner(x)
  local r = target(x)
  return r
end

function _G.ljbt_basic_cb(x)
  local r = R.call_t(ljbt_basic_inner, x)
  return r
end

function _G.ljbt_basic(x)
  local r = R.call(ljbt_basic_cb, x)
  return r
end

-- rec (seq 2): Lua and Rust recurse into each other, RECURSION levels of
-- Rust; the target is called by the innermost ljbt_rec.
local RECURSION = 30

function _G.ljbt_rec(n, x)
  if n == 0 then
    local r = target(x)
    return r
  end
  local r = R.call(ljbt_rec, n - 1, x)
  return r
end

-- error (seq 3): pcall -> Lua -> call (Rust, lua_call is unprotected) ->
-- Lua error().  The stop is in lj_err_throw.
local function thrower()
  H.oracle(3) error('ljbt-boom')
end

function _G.ljbt_err()
  local r = R.call(thrower)
  return r
end

-- ffi (seq 4): a Rust function called through FFI from a hot loop (a
-- trace under JIT); for the last argument it calls box_space_id_by_name.
function _G.ljbt_ffi(x)
  local args = {}
  for i = 1, 99 do args[i] = 2000 end
  args[100] = x
  local r
  for i = 1, 100 do
    r = lib.ljbt_ffi_hot(args[i])
  end
  return r
end

-- tnt scenarios: tlua (seq 5), proc through box.func (seq 6), proc
-- through an iproto call (seq 7), fibers created from Rust (seq 8).
function _G.ljbt_from_tlua(x)
  local r = target(x)
  return r
end

function _G.ljbt_from_proc(x)
  local r = target(x)
  return r
end

function _G.ljbt_tlua(x)
  local r = ljbt_tnt_call(x)
  return r
end

local PROC = 'ljbt_tnt.ljbt_proc'

function _G.ljbt_proc_func(x)
  local r = box.func[PROC]:call({x})
  return r
end

function _G.ljbt_proc_iproto(x)
  local c = require('net.box').connect('unix/:' .. workdir .. '/s.sock')
  local r = c:call(PROC, {x})
  c:close()
  return r
end

function _G.ljbt_fiber_body()
  while not LJBT_STOP do
    fiber.sleep(0.01)  -- FIBER_SLEEP_LINE
  end
end

local function tnt_setup()
  package.cpath = tnt_dir .. '/lib?.so;' .. package.cpath
  require('ljbt_tnt')
  box.schema.func.create(PROC, {language = 'C', if_not_exists = true})
  box.schema.user.grant('guest', 'execute', 'function', PROC,
                        {if_not_exists = true})
end

local RUN = {}

function RUN.basic()
  local ok, r = pcall(ljbt_basic, 4001)
  H.check(ok, r, 1)
end

function RUN.rec()
  local ok, r = pcall(ljbt_rec, RECURSION, 4002)
  H.check(ok, r, 2)
end

function RUN.error()
  local ok, err = pcall(ljbt_err)
  result({scenario = 'error', ok = ok, err = tostring(err)})
end

function RUN.ffi()
  -- Setting hotloop (to its default) resets the hot counters: LuaJIT
  -- shares 64 of them between all loops by PC hash, and the random
  -- penalty an aborted trace of another loop left in the slot could keep
  -- this loop out of a trace for all its 100 iterations.
  jit.opt.start('hotloop=56')
  local ok, r = pcall(ljbt_ffi, 4004)
  H.check(ok, r, 4)
end

function RUN.tlua()
  local ok, r = pcall(ljbt_tlua, 4005)
  H.check(ok, r, 5)
end

function RUN.proc()
  local ok, r = pcall(ljbt_proc_func, 4006)
  H.check(ok, r, 6)
end

function RUN.iproto()
  local ok, r = pcall(ljbt_proc_iproto, 4007)
  H.check(ok, r, 7)
end

function RUN.fibers()
  local lua_id = ljbt_tnt_spawn_lua()
  local rust_id = ljbt_tnt_spawn_rust()
  -- Both fibers sleep now; the probe reads their ids from here.
  result({scenario = 'fibers', lua = lua_id, rust = rust_id})
  local ok, r = pcall(target, 4008)
  H.check(ok, r, 8)
  LJBT_STOP = true
  ljbt_tnt_stop()
  fiber.sleep(0.05)
end

local function D()
  for name in scenarios:gmatch('[^,]+') do
    if name == 'tlua' or name == 'proc' or name == 'iproto' or
        name == 'fibers' then
      if not package.loaded['ljbt_tnt'] then tnt_setup() end
    end
    assert(RUN[name], 'no scenario ' .. name)()
    result({scenario = name, done = true})
  end
end

D()
result({done = true})
os.exit(0)
