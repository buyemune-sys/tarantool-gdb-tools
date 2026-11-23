-- Layer 3 workload: mixed C/Lua activity for random stops (SIGPROF of
-- sysprof in mode D, which counts VM states and walks no stack; with
-- LJBT_TIMER, of setitimer).  Runs until killed.  Work directory:
-- LJBT_WORKDIR.
local fiber = require('fiber')
local json = require('json')
local netbox = require('net.box')

local workdir = assert(os.getenv('LJBT_WORKDIR'))
local sock = workdir .. '/s.sock'
box.cfg({
  listen = sock, work_dir = workdir, memtx_dir = workdir, wal_mode = 'none',
  log = workdir .. '/tarantool.log', memtx_memory = 32 * 1024 * 1024,
})
if os.getenv('LJBT_JIT_MODE') == 'off' then jit.off() else jit.on() end
box.schema.user.grant('guest', 'super', nil, nil, {if_not_exists = true})
local s = box.schema.space.create('t', {if_not_exists = true})
s:create_index('pk', {if_not_exists = true})

-- Called over iproto (execute_lua_call -> lua_call) by the net.box fibers.
function _G.handle(id, payload)
  local t = {id = id, payload = payload}
  return t.id, #t.payload
end

s:on_replace(function(old, new) if new ~= nil then return new end end)

local function loop(name, f)
  fiber.create(function()
    fiber.name(name)
    local i = 0
    while true do
      f(i)
      i = i + 1
      if i % 50 == 0 then fiber.yield() end
    end
  end)
end

local conn = netbox.connect(sock)
assert(conn:ping())
for k = 1, 2 do
  loop('netbox' .. k, function(i) conn:call('handle', {i, 'p' .. i}) end)
end
loop('trigger', function(i) s:replace({i % 1000, 'v' .. i}) end)
local mt = {__serialize = function(self) return {id = self.id} end}
loop('json', function(i)
  local rows = {}
  for k = 1, 20 do rows[k] = setmetatable({id = k + i}, mt) end
  json.encode(rows)
end)
loop('sort', function(i)
  local t = {}
  for k = 1, 200 do t[k] = (k * 7919 + i) % 1009 end
  table.sort(t, function(a, b) return a < b end)
end)
loop('errors', function(i)
  pcall(box.error, box.error.ILLEGAL_PARAMS, 'bad ' .. i)
  pcall(error, 'lua error ' .. i)
end)

if os.getenv('LJBT_TIMER') then
  -- The window layer: SIGPROF every 1 ms of CPU time from setitimer, no
  -- sysprof (LuaJIT has none on arm64).  The handler is getpid, a C
  -- function that ignores its argument: gdb steps into it to stop under
  -- the frame of a signal.
  local ffi = require('ffi')
  ffi.cdef([[
    struct ljbt_timeval { long sec; long usec; };
    struct ljbt_itimerval { struct ljbt_timeval interval, value; };
    int setitimer(int which, const struct ljbt_itimerval *nv,
                  struct ljbt_itimerval *ov);
    typedef void (*ljbt_handler_t)(int);
    ljbt_handler_t signal(int signum, ljbt_handler_t handler);
    int getpid(void);
  ]])
  local SIGPROF, ITIMER_PROF = 27, 2
  ffi.C.signal(SIGPROF, ffi.cast('ljbt_handler_t', ffi.C.getpid))
  local it = ffi.new('struct ljbt_itimerval', {{0, 1000}, {0, 1000}})
  assert(ffi.C.setitimer(ITIMER_PROF, it, nil) == 0)
else
  assert(misc.sysprof.start({mode = 'D', interval = 1}))
end
while true do fiber.sleep(1) end
