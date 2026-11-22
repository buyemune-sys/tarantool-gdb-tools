-- Layer 5: the tail call heuristic, positive and negative cases.  gdb
-- stops in box_index_get (memtx t:get() goes there through FFI); every
-- stop also has space_mt.get tail-calling get_ffi (builtin schema.lua).
local workdir = assert(os.getenv('LJBT_WORKDIR'))
box.cfg({
  listen = workdir .. '/s.sock', work_dir = workdir, memtx_dir = workdir,
  wal_mode = 'none', log = workdir .. '/tarantool.log',
  memtx_memory = 32 * 1024 * 1024,
})
if os.getenv('LJBT_JIT_MODE') == 'off' then jit.off() else jit.on() end
local t = box.schema.space.create('t', {if_not_exists = true})
t:create_index('pk', {if_not_exists = true})
for i = 1, 4 do t:replace({i}) end

function _G.lj_c(x)
  local r = box.space.t:get(x)
  return r
end
function _G.lj_b(x) return lj_c(x) end
function _G.lj_a(x) return lj_b(x) end

local function run()
  local r1 = lj_a(1)            -- stop 1: lj_a and lj_b are gone
  local r2 = lj_c(2)            -- stop 2: a plain call
  local f = (r1 ~= nil) and lj_c or lj_b
  local r3 = f(3)               -- stop 3: a local computed in an expression
  local r4 = (function() return lj_c end)()(4)  -- stop 4: a call result
  return r1, r2, r3, r4
end

box.session.id()                -- marker: gdb arms box_index_get here
run()
os.exit(0)
