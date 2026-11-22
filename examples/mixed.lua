-- Scenarios for lj-bt.
-- Usage: tarantool mixed.lua <scenario> <jit on|off> <main|fiber>
-- gdb stops: `lbox_session_id` is the marker called right before the
-- scenario; then the scenario hits box_index_get (memtx get, FFI),
-- lbox_index_get (vinyl get, Lua C function), lj_err_throw or
-- box_space_id_by_name (FFI from a trace).
local fio = require('fio')
local fiber = require('fiber')
local ffi = require('ffi')

local here = fio.dirname(fio.abspath(arg[0]))
local scen, jitmode, where = arg[1], arg[2] or 'on', arg[3] or 'main'
local dir = here .. '/w/' .. scen .. '-' .. jitmode .. '-' .. where
fio.rmtree(dir)
fio.mktree(dir)
box.cfg({
  listen = dir .. '/s.sock', work_dir = dir, memtx_dir = dir,
  vinyl_dir = dir, wal_dir = dir, log = dir .. '/tarantool.log',
})
if jitmode == 'off' then jit.off() else jit.on() end

local t = box.schema.space.create('t', {if_not_exists = true})
t:create_index('pk', {if_not_exists = true})
local v = box.schema.space.create('v', {engine = 'vinyl',
                                        if_not_exists = true})
v:create_index('pk', {if_not_exists = true})
for i = 1, 10 do
  t:replace({i, 'm' .. i})
  v:replace({i, 'v' .. i})
end
ffi.cdef([[uint32_t box_space_id_by_name(const char *name, uint32_t len);]])

local S = {}

-- main chunk -> table.sort (C) -> comparator (Lua) -> space:get (C).
local function cmp(a, b)
  box.space.t:get(a)
  box.space.v:get(b)
  return a < b
end

function S.sort()
  local arr = {3, 1, 2}
  table.sort(arr, cmp)
  return arr
end

-- pcall is a fast function: no C frame of its own.
function S.pcall()
  local function f(k)
    return box.space.v:get(k), box.space.t:get(k)
  end
  return pcall(f, 1)
end

-- A C function inside a coroutine.
function S.coro()
  local co = coroutine.create(function(k)
    local x = box.space.v:get(k)
    coroutine.yield(x)
    return box.space.t:get(k)
  end)
  coroutine.resume(co, 1)
  return coroutine.resume(co, 2)
end

-- An error thrown from C under pcall.
function S.error()
  return pcall(box.error, box.error.ILLEGAL_PARAMS, 'lj-bt test')
end

-- A hot loop compiled to a trace calling a box function via FFI.
function S.trace()
  local names = {}
  for i = 1, 2000 do names[i] = 't' end
  names[1500] = 'tttttttt'      -- gdb: break box_space_id_by_name if len == 8
  local C = ffi.C
  local function look(n)
    local len = #n
    if len > 100 then return 0 end  -- a guard: a snapshot inside look()
    return C.box_space_id_by_name(n, len)
  end
  local s = 0
  for i = 1, 2000 do
    s = s + look(names[i])
  end
  local ti = require('jit.util').traceinfo(1)
  print('trace 1:', ti and ti.nins, ti and ti.link)
  return s
end

-- A fiber asleep in the middle of a mixed chain; the main fiber stops.
function S.sleep()
  local f = fiber.new(function()
    local arr = {2, 1}
    table.sort(arr, function(a, b)
      fiber.sleep(100)
      return a < b
    end)
  end)
  f:name('sleeper')
  fiber.yield()
  return box.space.v:get(1)
end

-- Tail calls: lj_a -> lj_b -> lj_c by CALLT, lj_c calls t:get().
function _G.lj_c(x)
  local r = box.space.t:get(x)
  return r
end
function _G.lj_b(x) return lj_c(x) end
function _G.lj_a(x) return lj_b(x) end

function S.tail()
  local r1 = lj_a(1)            -- stop 1: a and b are gone
  local r2 = lj_c(2)            -- stop 2: a plain call, nothing to add
  local f = (r1 ~= nil) and lj_c or lj_b
  local r3 = f(3)               -- stop 3: a local computed in an expression
  local r4 = (function() return lj_c end)()(4)  -- stop 4: unknowable
  return r1, r2, r3, r4
end

io.stdout:setvbuf('no')
box.session.id()                -- marker for gdb
if where == 'fiber' then
  local f = fiber.new(S[scen])
  f:name('scenario')
  f:set_joinable(true)
  print(f:join())
else
  print(S[scen]())
end
os.exit(0)
