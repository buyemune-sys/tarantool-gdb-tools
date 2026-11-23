-- Prepended to every scenario script of tests/dbg: what the Lua side
-- knows, written to oracle.json in the working directory for the tests
-- to compare the extension with.
local json = require('json')

local ORACLE = {}

-- A function as debug.getinfo sees it.
local function fninfo(f)
    local i = debug.getinfo(f, 'Su')
    return {
        what = i.what, source = i.source, line = i.linedefined,
        lastline = i.lastlinedefined, nups = i.nups,
        addr = tostring(f):match('0x%x+'),
    }
end

-- The frames of the running coroutine from the function that calls
-- stack() down, as debug.getinfo sees them.
local function stack()
    local frames = {}
    for level = 2, 100 do
        local i = debug.getinfo(level, 'Sf')
        if i == nil then
            break
        end
        table.insert(frames, {
            what = i.what, line = i.linedefined,
            addr = tostring(i.func):match('0x%x+'),
        })
    end
    return frames
end

local function oracle_write()
    local f = assert(io.open('oracle.json', 'w'))
    f:write(json.encode(ORACLE))
    f:close()
end
