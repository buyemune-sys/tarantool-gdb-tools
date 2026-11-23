-- Layer 5: workloads that crash sysprof in mode C (it walks the Lua stack
-- in the windows where it is inconsistent).  Derived from the prodlike /
-- hostchain experiments.
-- Usage: tarantool crash.lua <trigger|json|netbox|sortlong> <seconds>
local fiber = require('fiber')
local clock = require('clock')
local json = require('json')

local workdir = assert(os.getenv('LJBT_WORKDIR'))
local what, secs = arg[1], tonumber(arg[2] or '30')
local sock = workdir .. '/s.sock'
box.cfg({
  listen = sock, work_dir = workdir, memtx_dir = workdir, wal_mode = 'none',
  log = workdir .. '/tarantool.log', memtx_memory = 64 * 1024 * 1024,
})
box.schema.user.grant('guest', 'super', nil, nil, {if_not_exists = true})
local s = box.schema.space.create('t', {if_not_exists = true})
s:create_index('pk', {if_not_exists = true})

function _G.handle(id, payload)  -- HANDLE_DEF
  local t = {id = id, payload = payload, ts = clock.time()}
  return t.id, #t.payload
end

local deadline = clock.monotonic() + secs
local W = {}

function W.netbox()
  local conn = require('net.box').connect(sock)
  assert(conn:ping())
  for _ = 1, 16 do
    fiber.create(function()
      local ops = 0
      while clock.monotonic() < deadline do
        conn:call('handle', {ops, 'payload-' .. ops})
        ops = ops + 1
      end
    end)
  end
end

function W.trigger()
  s:on_replace(function(old, new) if new ~= nil then return new end end)
  fiber.create(function()
    local i = 0
    while clock.monotonic() < deadline do
      s:replace({i % 100000, 'value-' .. i})  -- TRIGGER_LINE
      i = i + 1
      if i % 1000 == 0 then fiber.yield() end
    end
  end)
end

function W.json()
  local mt = {__serialize = function(self)  -- SERIALIZE_DEF
    return {id = self.id, name = self.name}
  end}
  fiber.create(function()
    local i = 0
    while clock.monotonic() < deadline do
      local rows = {}
      for k = 1, 50 do rows[k] = setmetatable({id = k, name = 'n' .. k}, mt) end
      json.encode(rows)  -- JSON_LINE
      i = i + 1
      if i % 100 == 0 then fiber.yield() end
    end
  end)
end

function W.sortlong()
  local t = {}
  while clock.monotonic() < deadline do
    for i = 1, 200000 do t[i] = (i * 7919) % 200003 end
    table.sort(t, function(a, b) return a < b end)  -- SORT_LINE
  end
end

assert(misc.sysprof.start({mode = 'C', interval = 10,
                           path = workdir .. '/cpu.bin'}))
W[what]()  -- MAIN_LINE
while clock.monotonic() < deadline do fiber.sleep(0.5) end
misc.sysprof.stop()
print('survived', what)
os.exit(0)
