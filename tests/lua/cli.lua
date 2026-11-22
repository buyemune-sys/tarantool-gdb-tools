-- Layer cli: one scenario, numbered stops.  Each stop is a call of
-- box.session.exists(5000 + n) (the gdb side, cli_gdb.py, breaks there
-- and acts on n), some arm a second breakpoint first.  Work directory:
-- LJBT_WORKDIR.
local fiber = require('fiber')
local workdir = assert(os.getenv('LJBT_WORKDIR'))
local sock = workdir .. '/s.sock'
box.cfg({
  listen = sock, work_dir = workdir, memtx_dir = workdir, wal_mode = 'none',
  log = workdir .. '/tarantool.log', memtx_memory = 32 * 1024 * 1024,
})
box.schema.user.grant('guest', 'super', nil, nil, {if_not_exists = true})
jit.off()

local function stop(n) return box.session.exists(5000 + n) end

local function write(name, text)
  local fh = assert(io.open(workdir .. '/' .. name, 'w'))
  fh:write(text)
  fh:close()
end

-- 1: a mixed chain in the main fiber: flags, idempotency, threads.
local function cmp(a, b) stop(1) return a < b end
table.sort({2, 1}, cmp)

-- 2: a suspended coroutine, lj-bt <its lua_State *>.
CO = coroutine.create(function(x)
  local y = x + 1
  coroutine.yield(y)                     -- CO_YIELD
  return y
end)
coroutine.resume(CO, 1)
write('co.addr', tostring(CO):match('0x%x+'))
stop(2)

-- 3: more C frames than lj-bt walks by default: nested table.sort.
local depth = 0
local function nest(a, b)
  depth = depth + 1
  if depth < 90 then table.sort({2, 1}, nest) else stop(3) end
  return a < b
end
table.sort({2, 1}, nest)

-- 4: the tail call heuristic names a local of a function whose varinfo
-- is longer than 8 KB, and an upvalue past 4 KB of uvinfo.
function _G.lj_c(x) return stop(x) end
function _G.lj_b(x) return lj_c(x) end
function _G.lj_a(x) return lj_b(x) end
local src = {'local lj_a = ...\nreturn function()'}
for i = 1, 70 do
  src[#src + 1] = ('local local_with_a_rather_long_name_number_%03d_%s = %d')
    :format(i, string.rep('x', 100), i)
end
src[#src + 1] = 'local f = lj_a\nlocal r = f(4)\nreturn r\nend'
assert(loadstring(table.concat(src, '\n'), '=bigvarinfo'))(lj_a)()

-- At most 60 upvalues (LJ_MAX_UPVAL): 55 names of 150 bytes are 8 KB.
local usrc = {}
for i = 1, 55 do
  usrc[#usrc + 1] = ('local upvalue_with_a_rather_long_name_%03d_%s = %d')
    :format(i, string.rep('y', 115), i)
end
usrc[#usrc + 1] = 'local uv_last = ...\nreturn function()'
for i = 1, 55 do
  usrc[#usrc + 1] = ('local _ = upvalue_with_a_rather_long_name_%03d_%s')
    :format(i, string.rep('y', 115))
end
usrc[#usrc + 1] = 'local r = uv_last(5)\nreturn r\nend'
assert(loadstring(table.concat(usrc, '\n'), '=biguvinfo'))(lj_a)()

-- 6: the VM's math helper, a C-ABI function inside the VM code.
stop(6)
local fl = math.floor(1.5)

-- 7: inside an asm fast function (lj_ff_math_abs): the VM runs there,
-- L->base is behind and only a note can say so.
stop(7)
local ab = math.abs(-1)

-- 10: a Lua C function's lua_call returns into the VM: lj_vm_return,
-- an exit window.
stop(10)
table.sort({2, 1}, function(a, b) return a < b end)

-- 8: a request handled in the iproto thread while tx waits.
stop(8)
local conn = require('net.box').connect(sock)
conn:ping()
conn:close()

-- 9: a fiber asleep while another one stops: tt-fiber, then stepi.
local sleeper = fiber.new(function()
  table.sort({2, 1}, function(a, b) fiber.sleep(100) return a < b end)
end)
sleeper:name('cli-sleeper')
fiber.yield()
stop(9)

os.exit(0)
