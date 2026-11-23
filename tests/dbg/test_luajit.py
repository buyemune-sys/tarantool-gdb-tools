# Tests of luajit_dbg.py against tarantool: every command, live and on
# a core, the output compared with what the Lua side or gdb itself says.
#
# A port of test/tarantool-debugger-tests/debug-extension-tests.py of
# tarantool/luajit (gdb only) from the luajit binary to tarantool, with
# the content checked against oracles instead of patterns where one is
# at hand: debug.getinfo for functions and frames, jit.bc for bytecode,
# jit.dump for trace IR, gdb expressions for addresses and fields.

import re
import unittest

import harness as H
from harness import DbgCase, Scenario, G, GG, gcval, mref

RX_ADDR = r'0x[0-9a-f]+'
NUM = 'integer' if H.IS_DUALNUM else 'number'
COMMANDS = ['lj-arch', 'lj-bc', 'lj-ctype', 'lj-func', 'lj-gc', 'lj-gco',
            'lj-gctop', 'lj-ir', 'lj-jslots', 'lj-proto', 'lj-stack',
            'lj-state', 'lj-str', 'lj-tab', 'lj-trace', 'lj-tv']
ARG_COMMANDS = ['lj-bc', 'lj-ctype', 'lj-func', 'lj-gco', 'lj-ir',
                'lj-proto', 'lj-str', 'lj-tab', 'lj-trace', 'lj-tv']


def hexint(s):
    return int(s, 16)


def base(i):
    '''The i-th argument of the C function at the stop.'''
    return '(L->base + {})'.format(i)


# The GC is stopped after a full cycle: the state is PAUSE, and nothing
# moves between the commands.
QUIET_GC = (
    "collectgarbage('collect')\n"
    "collectgarbage('stop')\n"
)

GC_FIELDS = ('total', 'threshold', 'debt', 'estimate', 'stepmul', 'pause')
GC_LISTS = (('root', 0), ('gray', 0), ('grayagain', 0), ('weak', 0),
            ('mmudata', 1))


def gc_cmds(L='L'):
    g = G(L)
    cmds = [('gc', 'lj-gc'), ('state', 'lj-state')]
    cmds += [('gc.' + f, 'eval:{}->gc.{}'.format(g, f)) for f in GC_FIELDS]
    cmds += [('gc.sweepstr', 'eval:{}->gc.sweepstr'.format(g)),
             ('gc.strmask', 'eval:{}->strmask'.format(g)),
             ('gc.state', 'eval:{}->gc.state'.format(g))]
    cmds += [('gc.' + f, 'eval:$dbg_gclen({}->gc.{}, {})'.format(g, f, r))
             for f, r in GC_LISTS]
    return cmds


def stack_cmds(key, L='L', arg=None):
    '''lj-stack and the fields of the lua_State to check it with.'''
    cmd = 'lj-stack' + ('' if arg is None else ' ' + arg)
    return [
        (key, cmd),
        (key + '.stack', 'eval:(long){}'.format(mref(L + '->stack',
                                                     'TValue'))),
        (key + '.maxstack', 'eval:(long){}'.format(
            mref(L + '->maxstack', 'TValue'))),
        (key + '.stacksize', 'eval:{}->stacksize'.format(L)),
        (key + '.base', 'eval:(long){}->base'.format(L)),
        (key + '.top', 'eval:(long){}->top'.format(L)),
    ]


BASIC = Scenario('basic', QUIET_GC + 'print(1)\n', ['break lj_cf_print'],
                 [('arch', 'lj-arch'), ('L', 'eval:(long)L'),
                  ('tarantool_L', 'eval:(long)tarantool_L')] +
                 gc_cmds() + stack_cmds('stack') +
                 stack_cmds('stackL', arg='L') +
                 [('noarg.' + c, c) for c in ARG_COMMANDS])

DEFERRED = Scenario('deferred', QUIET_GC + 'print(1)\n',
                    ['break lj_cf_print'],
                    [('arch', 'lj-arch'), ('state', 'lj-state')],
                    deferred=True)


class TestLoad(DbgCase):
    SCENARIOS = (BASIC, DEFERRED)

    def check_load(self, text):
        lines = [s for s in text.split('\n') if s]
        inited = [s for s in lines if s.endswith(' command initialized')]
        self.assertEqual([s.split()[0] for s in inited], COMMANDS)
        self.assertIn('LuaJIT debug extension is successfully loaded', lines)

    def test_load(self):
        for mode in BASIC.modes():
            res = self.result(BASIC, mode)
            self.assertEqual(res['load_err'], [])
            self.check_load(res['load'])
        self.assertNoPyError(BASIC)

    def test_deferred(self):
        # Sourced before `file`: it waits for the objfile.
        for mode in DEFERRED.modes():
            res = self.result(DEFERRED, mode)
            self.assertIn('initialization is postponed until libluajit '
                          'objfile is loaded', res['load'])
            self.check_load(res['load'])
        self.assertEqual(self.out(DEFERRED, 'arch'),
                         self.out(BASIC, 'arch'))
        self.assertEqual(self.out(DEFERRED, 'state'),
                         self.out(BASIC, 'state'))
        self.assertNoPyError(DEFERRED)

    def test_missing_arg(self):
        for c in ARG_COMMANDS:
            for mode, err in self.errs(BASIC, 'noarg.' + c):
                self.assertEqual(err, 'Argument is required',
                                 '{} ({})'.format(c, mode))


class TestArchStateGC(DbgCase):
    SCENARIOS = (BASIC,)

    def test_arch(self):
        self.assertEqual(self.out(BASIC, 'arch').strip(),
                         'LJ_64: True, LJ_GC64: {}, LJ_DUALNUM: {}'.format(
                             H.IS_GC64, H.IS_DUALNUM))

    def test_state(self):
        # A C function called by the VM, the GC paused by the script.
        self.assertEqual(self.out(BASIC, 'state').strip().split('\n'),
                         ['VM state: CFUNC', 'GC state: PAUSE',
                          'JIT state: IDLE'])

    def test_gc(self):
        check_gc(self, BASIC, 'PAUSE')


# No Lua state to take G from: before tarantool_L is set, and in a
# thread with no Lua (tarantool_L is __thread since 3.7: NULL there; a
# plain global before, the same in every thread).
NO_L = 'no lua_State: neither globalL nor tarantool_L is set in this thread'
MAIN_COMMANDS = ['lj-state', 'lj-gc', 'lj-stack', 'lj-jslots']
EARLY = Scenario('early', 'print(1)\n', ['break tarantool_lua_init'],
                 [('tarantool_L', 'eval:(long)tarantool_L')] +
                 [(c, c) for c in MAIN_COMMANDS])
SWITCH = ('python inf = gdb.selected_inferior(); [t for t in inf.threads() '
          'if (t.ptid[1] {} inf.pid)][0].switch()')
THREADS = Scenario('threads', '''
local fio = require('fio')
fio.stat('/')                   -- a coio thread does it
print(1)
''', ['break lj_cf_print'],
    # gcore leaves the last thread selected: pick both explicitly.
    [('to_main', SWITCH.format('==')),
     ('main.L', 'eval:(long)tarantool_L')] +
    [('main.' + c, c) for c in MAIN_COMMANDS[:3]] +
    [('to_coio', SWITCH.format('!=')),
     ('coio.L', 'eval:(long)tarantool_L')] +
    [('coio.' + c, c) for c in MAIN_COMMANDS])


class TestNoLuaState(DbgCase):
    SCENARIOS = (EARLY, THREADS)

    def test_early(self):
        self.assertEqual(self.value(EARLY, 'tarantool_L'), 0)
        for c in MAIN_COMMANDS:
            for mode, err in self.errs(EARLY, c):
                self.assertEqual(err, NO_L, '{} ({})'.format(c, mode))

    def test_thread_without_lua(self):
        main = self.value(THREADS, 'main.L')
        self.assertNotEqual(main, 0)
        if self.value(THREADS, 'coio.L') == 0:
            # 3.7+: no Lua in this thread.
            for c in MAIN_COMMANDS:
                for mode, err in self.errs(THREADS, 'coio.' + c):
                    self.assertEqual(err, NO_L, '{} ({})'.format(c, mode))
        else:
            # The global tarantool_L: the Lua of tx from every thread.
            self.assertEqual(self.value(THREADS, 'coio.L'), main)
            for c in MAIN_COMMANDS[:3]:
                self.assertEqual(self.out(THREADS, 'coio.' + c),
                                 self.out(THREADS, 'main.' + c))
        self.assertNoPyError(THREADS)


def check_gc(case, scen, state):
    v = lambda k: case.value(scen, 'gc.' + k)  # noqa: E731
    want = ['GC stats: ' + state]
    want += ['\t{}: {}'.format(f, v(f)) for f in GC_FIELDS]
    want += ['\tsweepstr: {}/{}'.format(v('sweepstr'), v('strmask') + 1)]
    want += ['\t{}: {} objects'.format(f, v(f)) for f, _ in GC_LISTS]
    case.assertEqual(case.out(scen, 'gc').rstrip('\n').split('\n'), want)


# lj-stack ##########################################################

RX_SLOT = re.compile(r'^(0x[0-9a-f]+)(?::(0x[0-9a-f]+))?\s+'
                     r'\[([S ])([B ])([T ])([M ])\] (VALUE|FRAME): (.*)$')
RX_FREE = re.compile(r'^(0x[0-9a-f]+):(0x[0-9a-f]+) \[    \] (\d+) slots: '
                     r'Free stack slots$')
RX_RED = re.compile(r'^-+ Red zone:\s+(\d+) slots -+$')
RX_STACK = re.compile(r'^-+ Stack:\s+(\d+) slots -+$')
RX_FRAME = re.compile(r'^\[(\w+)\] delta=(\d+), (.*)$')


def parse_stack(text):
    '''lj-stack output: {'red': n, 'red_slots': [addr], 'size': n,
    'free': (from, to, n), 'slots': [dict]} with the slots in the order
    printed (from the top down).'''
    st = {'red_slots': [], 'slots': []}
    part = None
    for s in text.rstrip('\n').split('\n'):
        m = RX_RED.match(s)
        if m:
            st['red'] = int(m.group(1))
            part = 'red'
            continue
        m = RX_STACK.match(s)
        if m:
            st['size'] = int(m.group(1))
            part = 'stack'
            continue
        m = RX_FREE.match(s)
        if m:
            st['free'] = (hexint(m.group(1)), hexint(m.group(2)),
                          int(m.group(3)))
            continue
        m = RX_SLOT.match(s)
        if not m:
            raise AssertionError('unexpected lj-stack line: ' + s)
        slot = {
            'addr': hexint(m.group(2) or m.group(1)),
            'lo': hexint(m.group(1)),
            'S': m.group(3) == 'S', 'B': m.group(4) == 'B',
            'T': m.group(5) == 'T', 'M': m.group(6) == 'M',
            'kind': m.group(7), 'text': m.group(8),
        }
        if slot['kind'] == 'FRAME' and slot['text'] != 'dummy L':
            f = RX_FRAME.match(slot['text'])
            if not f:
                raise AssertionError('unexpected frame: ' + s)
            slot['type'], slot['delta'], slot['func'] = \
                f.group(1), int(f.group(2)), f.group(3)
        if part == 'red':
            st['red_slots'].append(slot)
        else:
            st['slots'].append(slot)
    return st


def check_stack(case, scen, key):
    '''lj-stack against the lua_State fields: sizes, marks, every slot
    from L->top down to the bottom exactly once.  Returns the parsed
    stack.'''
    st = parse_stack(case.out(scen, key))
    v = lambda k: case.value(scen, key + '.' + k)  # noqa: E731
    stack, maxstack = v('stack'), v('maxstack')
    base, top, size = v('base'), v('top'), v('stacksize')
    tv = 8
    nslots = (maxstack - stack) // tv
    # lj_state.c: stacksize = n + 1 + LJ_STACK_EXTRA, maxstack = stack +
    # n; the red zone is the LJ_STACK_EXTRA slots above maxstack, 7 or 8
    # with LJ_FR2 depending on the LuaJIT version.
    red = size - 1 - nslots
    case.assertEqual(st['red'], red, 'red zone size')
    case.assertEqual([s['addr'] for s in st['red_slots']],
                     [maxstack + tv * i for i in range(red, 0, -1)])
    case.assertEqual(st['size'], nslots)
    slots = st['slots']
    case.assertEqual(slots[0]['addr'], maxstack)
    case.assertTrue(slots[0]['M'])
    case.assertEqual(st['free'], (top + tv, maxstack - tv,
                                  (maxstack - top) // tv - 1))
    # From the top down, contiguous: a frame link takes two slots with
    # LJ_FR2 (shown as lo:addr), one without.
    expect = top
    for s in slots[1:]:
        case.assertEqual(s['addr'], expect, 'slot at {:#x}'.format(expect))
        expect = s['lo'] - tv
        case.assertEqual(s['B'], s['addr'] == base)
        case.assertEqual(s['T'], s['addr'] == top)
        case.assertFalse(s['M'])
    bottom = slots[-1]
    case.assertEqual(bottom['text'], 'dummy L')
    case.assertTrue(bottom['S'])
    case.assertEqual(bottom['lo'], stack)
    case.assertEqual(sum(1 for s in slots if s['S']), 1)
    return st


def frame_funcs(st):
    '''The frames of a parsed lj-stack, from the top, as (type, what,
    address): what is Lua or C; a vararg frame is left out, it repeats
    the function of the frame below it.'''
    out = []
    for s in st['slots']:
        if s['kind'] != 'FRAME' or s['text'] == 'dummy L':
            continue
        if s['type'] == 'V':
            continue
        m = re.match(r'Lua function @ (0x[0-9a-f]+)', s['func'])
        if m:
            out.append((s['type'], 'Lua', hexint(m.group(1))))
        else:
            out.append((s['type'], 'C', None))
    return out


STACK = Scenario('stack', '''
local function inner(...)                                    -- @inner
    local va = {...}
    local t = setmetatable({}, {__index = function(t, k)     -- @index
        ORACLE.frames = stack()
        oracle_write()
        print(k, va[1])
    end})
    return t.key
end
local function middle(a, b)                                  -- @middle
    return (pcall(inner, a, b, 'x'))
end
local co = coroutine.wrap(function() middle(1, 2) end)       -- @co
co()
''', ['break lj_cf_print'],
    [('L', 'eval:(long)L'), ('main', 'eval:(long)tarantool_L')] +
    stack_cmds('co', arg='L') + stack_cmds('main', L='tarantool_L'))


class TestStack(DbgCase):
    SCENARIOS = (BASIC, STACK)

    def test_main(self):
        st = check_stack(self, BASIC, 'stack')
        funcs = [s for s in st['slots'] if s['kind'] == 'FRAME']
        self.assertIn('fast function print', funcs[0]['func'])
        self.assertTrue(any('"@{}":0'.format(BASIC.script_path())
                            in s['func'] for s in funcs[1:]))
        # lj-stack with no argument is the stack of the main coroutine,
        # the one the script runs in.
        self.assertEqual(self.value(BASIC, 'L'),
                         self.value(BASIC, 'tarantool_L'))
        self.assertEqual(self.out(BASIC, 'stack'), self.out(BASIC, 'stackL'))

    def test_coroutine(self):
        self.assertNotEqual(self.value(STACK, 'L'),
                            self.value(STACK, 'main'))
        st = check_stack(self, STACK, 'co')
        got = frame_funcs(st)
        want = [('Lua' if f['what'] in ('Lua', 'main') else 'C',
                 hexint(f['addr']) if f['what'] != 'C' else None)
                for f in STACK.oracle()['frames']]
        # The frames above the metamethod are print and its wrapper.
        addrs = [g[2] for g in got]
        self.assertIn(want[0][1], addrs)
        below = got[addrs.index(want[0][1]):]
        self.assertEqual([(w, a) for _, w, a in below], want)
        # The frame types: the metamethod is a continuation, inner runs
        # under pcall, the coroutine function is entered by a C frame.
        # (The P of a Lua frame is test_lua_frame_has_no_p.)
        self.assertEqual([re.sub('^LP$', 'L', t) for t, _, _ in below],
                         ['M', 'PP', 'L', 'L', 'CP'])

    def test_main_of_coroutine(self):
        st = check_stack(self, STACK, 'main')
        funcs = [s['func'] for s in st['slots'] if s['kind'] == 'FRAME' and
                 s['text'] != 'dummy L']
        self.assertIn('fast function coroutine.wrap.aux', funcs[0])

    def test_lua_frame_has_no_p(self):
        # FRAME_P is a bit of the frame type for C, continuation and
        # vararg frames; the link of a Lua frame is the return PC, whose
        # bit 2 is an address bit, not a protected call.
        n = 0
        for scen, key in ((BASIC, 'stack'), (STACK, 'co'), (STACK, 'main')):
            st = parse_stack(self.out(scen, key))
            for s in st['slots']:
                if s.get('type', '').startswith('L'):
                    self.assertEqual(s['type'], 'L', s['text'])
                    n += 1
        self.assertGreater(n, 4)


# lj-tv, lj-gco, lj-str, lj-tab ######################################

# The arguments of print, sorted in the LJ_T order; GC is the range of
# the collectable ones.
VALUES = Scenario('values', '''
local ffi = require('ffi')
local fiber = require('fiber')
local up = 'upvalue'
local function lfunc() return up end
local co = coroutine.create(function() end)
local mt = {}
local tab = setmetatable({10, 20, 'thirty', key = 'value', [5.5] = true}, mt)
local ud = newproxy(true)
local str = 'hello world'
local nul = 'a\\0b'
ORACLE.lfunc = fninfo(lfunc)
oracle_write()
''' + QUIET_GC + '''
print(nil, false, true, debug.upvalueid(lfunc, 1), str, co, lfunc, fiber.id,
      math.min, getmetatable(io.stdout).close, getmetatable(io.stdout).__gc,
      ffi.new('int *'), tab, ud, 42, 1.5, -0.25, 2^53, nul)
''', ['break lj_cf_print'], [])
NVALUES = 19
GC = range(4, 14)
for i in range(NVALUES):
    VALUES.cmds.append(('tv{}'.format(i), 'lj-tv ' + base(i)))
    VALUES.cmds.append(('addr{}'.format(i), 'eval:(long){}'.format(
        gcval(base(i)))))
for i in GC:
    VALUES.cmds.append(('gco{}'.format(i), 'lj-gco ' + gcval(base(i))))
VALUES.cmds += [
    ('uv', 'eval:(long)$dbg_gcref(((GCfuncL *){})->uvptr[0])'.format(
        gcval(base(6)))),
    ('cf', 'eval:(long)((GCfuncC *){})->f'.format(gcval(base(7)))),
    ('ff8', 'eval:((GCfuncC *){})->ffid'.format(gcval(base(8)))),
    ('ff9', 'eval:((GCfuncC *){})->ffid'.format(gcval(base(9)))),
    ('ff10', 'eval:((GCfuncC *){})->ffid'.format(gcval(base(10)))),
    ('ctypeid', 'eval:((GCcdata *){})->ctypeid'.format(gcval(base(11)))),
    ('str', 'lj-str (GCstr *){}'.format(gcval(base(4)))),
    ('str.hash', 'eval:((GCstr *){})->hash'.format(gcval(base(4)))),
    ('nul', 'lj-str (GCstr *){}'.format(gcval(base(18)))),
    ('tab', 'lj-tab (GCtab *){}'.format(gcval(base(12)))),
]
TAB = '((GCtab *){})'.format(gcval(base(12)))
for f in ('asize', 'hmask'):
    VALUES.cmds.append(('tab.' + f, 'eval:{}->{}'.format(TAB, f)))
VALUES.cmds += [
    ('tab.mt', 'eval:(long)$dbg_gcref({}->metatable)'.format(TAB)),
    ('tab.array', 'eval:(long)$dbg_mref({}->array)'.format(TAB)),
    ('tab.node', 'eval:(long)$dbg_mref({}->node)'.format(TAB)),
    ('sizeof.Node', 'eval:sizeof(Node)'),
]


class TestValues(DbgCase):
    SCENARIOS = (VALUES,)

    def expected(self):
        v = lambda k: self.value(VALUES, k)  # noqa: E731
        a = lambda i: v('addr{}'.format(i))  # noqa: E731
        lf = VALUES.oracle()['lfunc']
        ffi = []
        for i, name in ((8, 'math.min'), (9, 'io.method.close'),
                        (10, 'io.method.__gc')):
            ffi.append('fast function {} (#{})'.format(name, v('ff%d' % i)))
        return [
            'nil', 'false', 'true',
            'light userdata @ {:#x}'.format(v('uv')),
            'string "hello world" @ {:#x}'.format(a(4)),
            'thread @ {:#x}'.format(a(5)),
            'Lua function @ {:#x}, {} upvalues, "{}":{}'.format(
                a(6), lf['nups'], lf['source'], lf['line']),
            'C function @ {:#x}'.format(v('cf')),
        ] + ffi + [
            'cdata @ {:#x} [{}] <int *> 0x0'.format(a(11), v('ctypeid')),
            'table @ {:#x} (asize: {}, hmask: {:#x})'.format(
                a(12), v('tab.asize'), v('tab.hmask')),
            'userdata @ {:#x}'.format(a(13)),
            NUM + ' 42', 'number 1.5', 'number -0.25',
            'number 9007199254740992',
            'string "a" @ {:#x}'.format(a(18)),
        ]

    def test_tv(self):
        want = self.expected()
        self.assertEqual(int(VALUES.oracle()['lfunc']['addr'], 16),
                         self.value(VALUES, 'addr6'))
        for i in range(NVALUES):
            self.assertEqual(self.out(VALUES, 'tv%d' % i).rstrip('\n'),
                             want[i], 'argument {}'.format(i))
        self.assertNoPyError(VALUES)

    def test_gco(self):
        want = self.expected()
        for i in GC:
            self.assertEqual(self.out(VALUES, 'gco%d' % i).rstrip('\n'),
                             want[i], 'argument {}'.format(i))

    def test_str(self):
        self.assertEqual(self.out(VALUES, 'str').rstrip('\n'),
                         'String: "hello world" [11 bytes] with hash '
                         '{:#x}'.format(self.value(VALUES, 'str.hash')))

    @unittest.expectedFailure
    def test_str_nul(self):
        # Known: the payload is read as a C string, a NUL byte ends it.
        self.assertIn('"a\\000b" [3 bytes]', self.out(VALUES, 'nul'))

    def test_tab(self):
        v = lambda k: self.value(VALUES, k)  # noqa: E731
        lines = self.out(VALUES, 'tab').rstrip('\n').split('\n')
        self.assertEqual(lines[0], 'Metatable detected: {:#x}'.format(
            v('tab.mt')))
        asize, hmask = v('tab.asize'), v('tab.hmask')
        self.assertGreaterEqual(asize, 4)
        self.assertEqual(lines[1], 'Array part: {} slots'.format(asize))
        arr = v('tab.array')
        vals = ['nil', 'number 10', 'number 20', r'string "thirty" @ ']
        vals += ['nil'] * (asize - len(vals))
        for i in range(asize):
            m = re.match(r'^(0x[0-9a-f]+): \[(\d+)\]: (.*)$', lines[2 + i])
            self.assertIsNotNone(m, lines[2 + i])
            self.assertEqual(hexint(m.group(1)), arr + 8 * i)
            self.assertEqual(int(m.group(2)), i)
            self.assertTrue(m.group(3).startswith(
                vals[i].replace('number', NUM)), m.group(3))
        nodes = lines[2 + asize:]
        self.assertEqual(nodes[0], 'Hash part: {} nodes'.format(hmask + 1))
        self.assertEqual(len(nodes), hmask + 2)
        node, size = v('tab.node'), v('sizeof.Node')
        ptrs = [node + size * i for i in range(hmask + 1)]
        pairs = set()
        for i, s in enumerate(nodes[1:]):
            m = re.match(r'^(0x[0-9a-f]+): \{ (.*) \} => \{ (.*) \}; '
                         r'next = (0x[0-9a-f]+)$', s)
            self.assertIsNotNone(m, s)
            self.assertEqual(hexint(m.group(1)), ptrs[i])
            self.assertIn(hexint(m.group(4)), ptrs + [0])
            if m.group(2) != 'nil':
                pairs.add((re.sub(' @ .*', '', m.group(2)),
                           re.sub(' @ .*', '', m.group(3))))
        self.assertEqual(pairs, {('string "key"', 'string "value"'),
                                 ('number 5.5', 'true')})


# lj-func, lj-proto, lj-bc #########################################

PROTO = Scenario('proto', '''
local fiber = require('fiber')
local uvname = false
local function testf(...)
  local a = ...
  local s1 = a + 42
  uvname = "conststr"
  if a >= 42 then
    return a - s1
  end
end
local function testg(t, ...)
  local n = -7
  local s = 'str'
  local k = 1.5
  local nn = nil
  local g = print
  t.x = n
  local tt = {1, 2, n}
  local f = function() return s end
  for i = 1, 3 do n = n + i end
  for _, v in ipairs(t) do n = n + v end
  return f, tt, g, k, nn, select('#', ...), uvname
end
local out = assert(io.open('bc.txt', 'w'))
require('jit.bc').dump(testf, out)
require('jit.bc').dump(testg, out)
out:close()
ORACLE.testf = fninfo(testf)
ORACLE.testg = fninfo(testg)
oracle_write()
print(testf, testg, fiber.id, math.min)
''', ['break lj_cf_print'], [])
PC = '((BCIns *)$dbg_mref(((GCfuncL *){})->pc))'.format(gcval(base(0)))
PROTO.cmds += [
    ('func', 'lj-func ' + gcval(base(0))),
    ('funcg', 'lj-func ' + gcval(base(1))),
    ('proto', 'lj-proto (GCproto *)((char *){} - sizeof(GCproto))'.format(PC)),
    ('bc0', 'lj-bc ' + PC),
    ('bc6', 'lj-bc {} + 6'.format(PC)),
    ('cfunc', 'lj-func ' + gcval(base(2))),
    ('cf', 'eval:(long)((GCfuncC *){})->f'.format(gcval(base(2)))),
    ('ffunc', 'lj-func ' + gcval(base(3))),
    ('ffid', 'eval:((GCfuncC *){})->ffid'.format(gcval(base(3)))),
]

RX_LJBC = re.compile(r'^(\d{4}) (\S+)\s*(.*)$')
RX_JITBC = re.compile(r'^(\d{4}) (=>|  ) (\S+)\s*(.*)$')


def ljbc_operands(rest):
    '''Operands of an lj-bc line: [(mode, value)], the jump target as
    "=>N"; and the constant comment.'''
    rest, _, kc = rest.partition(' ; ')
    ops = re.findall(r'(\w+):\s+(=> \d{4}|[+-]?\d+)', rest)
    return [(m, v.replace(' ', '')) for m, v in ops], kc


def parse_jitbc(text):
    '''jit.bc dumps: {first line: [(pc, op, [operands], comment)]}.'''
    funcs, cur = {}, None
    for s in text.split('\n'):
        m = re.match(r'^-- BYTECODE -- .*:(\d+)-(\d+)$', s)
        if m:
            cur = funcs.setdefault(int(m.group(1)), [])
            continue
        m = RX_JITBC.match(s)
        if m and cur is not None:
            rest, _, kc = m.group(4).partition(';')
            ops = [x.replace(' ', '') for x in
                   re.findall(r'=> \d{4}|[+-]?\d+', rest)]
            cur.append((int(m.group(1)), m.group(3), ops, kc.strip()))
    return funcs


class TestProto(DbgCase):
    SCENARIOS = (PROTO,)

    def check_func(self, key, info):
        lines = self.out(PROTO, key).rstrip('\n').split('\n')
        self.assertEqual(lines[0], '"{}":{}-{}'.format(
            info['source'], info['line'], info['lastline']))
        want = parse_jitbc(PROTO.read('bc.txt'))[info['line']]
        got = []
        for s in lines[1:]:
            m = RX_LJBC.match(s)
            self.assertIsNotNone(m, s)
            ops, kc = ljbc_operands(m.group(3))
            got.append((int(m.group(1)), m.group(2), ops, kc))
        # jit.bc leaves out the function header at pc 0.
        self.assertIn(got[0][1], ('FUNCF', 'FUNCV'))
        self.assertEqual(len(got) - 1, len(want))
        for (pc, op, ops, kc), (wpc, wop, wops, wkc) in zip(got[1:], want):
            what = 'pc {}: {}'.format(pc, s)
            self.assertEqual((pc, op), (wpc, wop), what)
            self.assertEqual([v for _, v in ops], wops, 'pc {}'.format(pc))
            if wkc.startswith('"'):
                self.assertIn('string ' + wkc, kc, 'pc {}'.format(pc))
            elif re.match(r'^-?[\d.]+$', wkc):
                self.assertIn(' ' + wkc, kc, 'pc {}'.format(pc))
        return got

    def test_func(self):
        got = self.check_func('func', PROTO.oracle()['testf'])
        kcs = ' '.join(g[3] for g in got)
        self.assertIn('upvalue "uvname" @ ', kcs)
        self.assertIn('string "conststr" @ ', kcs)
        self.assertNoPyError(PROTO)

    def test_func_more(self):
        # testg: negative literals, constants of every kind, closures,
        # loops, compared with jit.bc as well.
        got = self.check_func('funcg', PROTO.oracle()['testg'])
        self.assertIn(('KSHORT', [('dst', '1'), ('lits', '-7')]),
                      [(op, ops) for _, op, ops, _ in got])

    def test_proto(self):
        self.assertEqual(self.out(PROTO, 'proto'), self.out(PROTO, 'func'))

    def test_bc(self):
        lines = self.out(PROTO, 'func').split('\n')
        self.assertEqual(self.out(PROTO, 'bc0').rstrip('\n'), lines[1][5:])
        # Without a function the jump target is relative to the pc.
        bc6 = self.out(PROTO, 'bc6').rstrip('\n')
        self.assertRegex(bc6, r'^JMP    rbase:   \d jump:  \+\d$')
        target = int(re.search(r'=> (\d{4})', lines[7]).group(1))
        self.assertEqual(int(bc6.split('+')[1]), target - 6)

    def test_cfunc(self):
        self.assertEqual(self.out(PROTO, 'cfunc').rstrip('\n'),
                         'C function @ {:#x}'.format(
                             self.value(PROTO, 'cf')))

    def test_ffunc(self):
        self.assertEqual(self.out(PROTO, 'ffunc').rstrip('\n'),
                         'fast function math.min (#{})'.format(
                             self.value(PROTO, 'ffid')))


FFUNKNOWN = Scenario('ffunknown', 'print(pcall)\n', ['break lj_cf_print'], [
    ('gco', 'lj-gco ' + gcval(base(0))),
    ('func', 'lj-func ' + gcval(base(0))),
    ('set242', 'set var ((GCfuncC *){})->ffid = 242'.format(gcval(base(0)))),
    ('gco242', 'lj-gco ' + gcval(base(0))),
    ('func242', 'lj-func ' + gcval(base(0))),
    ('set253', 'set var ((GCfuncC *){})->ffid = 253'.format(gcval(base(0)))),
    ('gco253', 'lj-gco ' + gcval(base(0))),
    ('func253', 'lj-func ' + gcval(base(0))),
], live_only='the commands write to the memory of the process')


class TestFastFunctionUnknown(DbgCase):
    SCENARIOS = (FFUNKNOWN,)

    def test_unknown(self):
        for c in ('gco', 'func'):
            self.assertRegex(self.out(FFUNKNOWN, c),
                             r'^fast function pcall \(#\d+\)\n$')
            for n in (242, 253):
                self.assertEqual(self.out(FFUNKNOWN, c + str(n)),
                                 'fast function unknown (#{})\n'.format(n))


# lj-gctop ##########################################################

# Five objects far larger than anything of tarantool's own, each made
# with the GC stopped between two collectgarbage('count'): the
# difference is what it took (a coroutine also has the closure of its
# recursion and an upvalue).
GCTOP = Scenario('gctop', '''
local ffi = require('ffi')
local tnew = require('table.new')
local function delta(f, ...)
    local before = collectgarbage('count')
    local v = f(...)
    return v, (collectgarbage('count') - before) * 1024
end
local function deep(n)
    local function r(k)
        if k == 0 then coroutine.yield() return 0 end
        return 1 + r(k - 1)
    end
    return r(n)
end
local function thread(n)
    local co = coroutine.create(deep)
    coroutine.resume(co, n)
    return co
end
''' + QUIET_GC + '''
local buf, dbuf = delta(ffi.new, 'char[?]', 3000000)
local str = ffi.string(buf, 2000000)
local cd, dcd = delta(ffi.new, 'struct { char a[1500000]; }')
local tab, dtab = delta(tnew, 150000, 0)
jit.off()                       -- no trace of the recursion
local co, dco = delta(thread, 10000)
jit.on()
ORACLE.sizes = {dbuf, -1, dcd, dtab, dco}
oracle_write()
print(buf, str, cd, tab, co)
''', ['break lj_cf_print'], [
    ('top5', 'lj-gctop 5'),
    ('top', 'lj-gctop'),
    ('top1', 'lj-gctop 1'),
    ('bad', 'lj-gctop x'),
    ('zero', 'lj-gctop 0'),
    ('count', 'eval:$dbg_gclen({0}->gc.root, 0) + '
     '$dbg_gclen({0}->gc.mmudata, 1) + {0}->strnum'.format(G())),
    ('total', 'eval:{}->gc.total'.format(G())),
    ('str.size', 'eval:sizeof(GCstr) + ((GCstr *){})->len + 1'.format(
        gcval(base(1)))),
] + [('gco%d' % i, 'lj-gco ' + gcval(base(i))) for i in range(5)])
RX_GCTOP = re.compile(r'^(\d+) GC objects, (\d+) bytes; the (\d+) largest:$')


class TestGCTop(DbgCase):
    SCENARIOS = (GCTOP,)

    def test_top(self):
        lines = self.out(GCTOP, 'top5').rstrip('\n').split('\n')
        m = RX_GCTOP.match(lines[0])
        self.assertIsNotNone(m, lines[0])
        # Every object: the lists of the GC and the strings.
        self.assertEqual(int(m.group(1)), self.value(GCTOP, 'count'))
        self.assertLess(int(m.group(2)), self.value(GCTOP, 'total'))
        self.assertEqual(int(m.group(3)), 5)
        sizes = GCTOP.oracle()['sizes']
        sizes[1] = self.value(GCTOP, 'str.size')    # see sizestring()
        want = sorted(((sizes[i], self.out(GCTOP, 'gco%d' % i).rstrip('\n'))
                       for i in range(5)), reverse=True)
        got = []
        for s in lines[1:]:
            m = re.match(r'^(\d+) bytes: (.*)$', s)
            self.assertIsNotNone(m, s)
            got.append((int(m.group(1)), m.group(2)))
        self.assertEqual([o for _, o in got], [o for _, o in want])
        for (size, obj), (wsize, _) in zip(got, want):
            # The thread: plus the closure and the upvalue of deep().
            slack = 128 if obj.startswith('thread') else 0
            self.assertLessEqual(abs(size - wsize), slack, obj)
        self.assertNoPyError(GCTOP)

    def test_amount(self):
        top = self.out(GCTOP, 'top').rstrip('\n').split('\n')
        self.assertEqual(RX_GCTOP.match(top[0]).group(3), '10')
        self.assertEqual(len(top), 11)
        sizes = [int(s.split()[0]) for s in top[1:]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        top5 = self.out(GCTOP, 'top5').rstrip('\n').split('\n')
        self.assertEqual(top[1:6], top5[1:])
        self.assertEqual(self.out(GCTOP, 'top1').rstrip('\n').split('\n')[1],
                         top5[1])
        self.assertEqual(self.errs(GCTOP, 'bad')[0][1],
                         'Amount must be a number: "x"')
        self.assertEqual(self.errs(GCTOP, 'zero')[0][1],
                         'Amount must be positive: "0"')


# lj-ctype and the cdata of lj-tv ##################################

CHAR_SIGNED = H.ARCH == 'arm64'         # Linux only: no macOS here.
CTYPES = [
    ('ffi.new("bool")', 'bool', None),
    ('ffi.new("char")', 'char', None),
    ('ffi.new("signed char")', ('signed ' if CHAR_SIGNED else '') + 'char',
     None),
    ('ffi.new("unsigned char")',
     ('unsigned ' if not CHAR_SIGNED else '') + 'char', None),
    ('ffi.new("int")', 'int', None),
    ('ffi.new("short")', 'short', None),
    ('ffi.new("unsigned")', 'unsigned int', None),
    ('ffi.new("int8_t")', ('signed ' if CHAR_SIGNED else '') + 'char', None),
    ('ffi.new("int16_t")', 'short', None),
    ('ffi.new("int32_t")', 'int', None),
    ('ffi.new("int64_t", -5)', 'int64_t', '-5LL'),
    ('ffi.new("uint8_t")', ('unsigned ' if not CHAR_SIGNED else '') + 'char',
     None),
    ('ffi.new("uint64_t", 7)', 'uint64_t', '7ULL'),
    ('ffi.new("float")', 'float', None),
    ('ffi.new("double")', 'double', None),
    ('ffi.new("long double")', 'long double', None),
    ('1i', 'complex', '0+1i'),
    ('ffi.new("complex float", 1, -2)', 'complex float', '1-2i'),
    ('ffi.new("const volatile int")', 'const volatile int', None),
    ('ffi.new("void *")', 'void *', None),
    ('ffi.new("void * __ptr32")', 'void * __ptr32', None),
    ('ffi.new("int &")', 'int &', None),
    ('ffi.typeof(1LL)', 'ctype', None),
    ('ffi.new("struct dbg_test")', 'struct dbg_test', None),
    ('ffi.new("struct {int a;}")', r'struct \d+', None),
    ('ffi.new("union {int a;}")', r'union \d+', None),
    ('ffi.new("enum {DBG_ENUM1}")', r'enum \d+', None),
    ('ffi.new("char [0]")', 'char [0]', None),
    ('ffi.new("int [1]")', 'int [1]', None),
    ('ffi.new("complex [2]")', 'complex [2]', None),
    ('ffi.new("complex float [3]")', 'complex float [3]', None),
    ('ffi.new("float __attribute__((vector_size(4)))")',
     'float __attribute__((vector_size(4)))', None),
    ('ffi.new("int (&)[5]")', 'int (&)[5]', None),
    ('ffi.new("int[?]", 6)', 'int [?]', None),
    ('ffi.C.dbg_getpid', 'void ()', None),
    ('ffi.new("int (*)()")', 'int (*)()', None),
    ('ffi.new("int (*(*)(void))[2]")', 'int (* (*)())[2]', None),
]
CTYPE = Scenario('ctype', '''
local ffi = require('ffi')
ffi.cdef[[
  struct dbg_test {int a;};
  void dbg_getpid(void) asm("getpid");
]]
print(
''' + ',\n'.join('  ' + c[0] for c in CTYPES) + ')\n',
    ['break lj_cf_print'], [])
CTS = '((CTState *)$dbg_mref({}->ctype_state))'.format(G())
for i in range(len(CTYPES)):
    CTYPE.cmds += [
        ('tv%d' % i, 'lj-tv ' + base(i)),
        ('ct%d' % i, 'lj-ctype &{}->tab[((GCcdata *){})->ctypeid]'.format(
            CTS, gcval(base(i)))),
        ('addr%d' % i, 'eval:(long){}'.format(gcval(base(i)))),
        ('id%d' % i, 'eval:((GCcdata *){})->ctypeid'.format(gcval(base(i)))),
    ]


class TestCType(DbgCase):
    SCENARIOS = (CTYPE,)

    def test_cdata(self):
        for i, (lua, name, val) in enumerate(CTYPES):
            rx = r'^cdata @ {:#x} \[{}\] <{}> {}$'.format(
                self.value(CTYPE, 'addr%d' % i),
                self.value(CTYPE, 'id%d' % i),
                name if '\\d' in name else re.escape(name),
                re.escape(val) if val else RX_ADDR)
            self.assertRegex(self.out(CTYPE, 'tv%d' % i).rstrip('\n'), rx,
                             lua)
        self.assertNoPyError(CTYPE)

    def test_ctype(self):
        for i, (lua, name, val) in enumerate(CTYPES):
            tv = self.out(CTYPE, 'tv%d' % i)
            ct = self.out(CTYPE, 'ct%d' % i).rstrip('\n')
            self.assertRegex(ct, r'^\[\d+\] <.*>$')
            self.assertIn(' ' + ct + ' ', tv, lua)


# lj-trace, lj-ir, lj-jslots ########################################

JIT_CASES = [
    # (marker, Lua code compiled into one trace, IR patterns)
    ('@loop', 'for _ = 1, 200 do end', [
        r'int SLOAD  \[L \] lit: #\d+ +lit: C?I',
        r'\+ int ADD    \[C \] ref: \d{4} ref: integer 1',
        r'>  int LE     \[N \] ref: \d{4} ref: integer 200',
        r'>  --- LOOP   \[S \]',
        r'   int PHI    \[S \] ref: \d{4} ref: \d{4}']),
    ('@numloop', 'local r = 0.1 for i = 1, 200 do r = i + r end', [
        r'\+ num ADD']),
    ('@carg', 'for i = 1, 200 do if i < 1000 then ffi.C.dbg_getpid2(i, 1LL) '
     'end end', [
        r'>  int LT     \[N \] ref: \d{4} ref: integer 1000',
        r'   nil CARG   \[N \] ref: \d{4} ref: integer 1',
        r'   int CALLXS \[S \] \[' + RX_ADDR + r'\]\(\{\d{4}\}, '
        r'\{integer 1\}\)']),
    # CALL(args): f(args), four times: the hot counters of the functions
    # share slots, two calls do not always make a trace.
    ('@neg', 'local function f(a) local x = -a return x end CALL(1.1)',
     [r'num FLOAD .* ref: nil  lit: '
      r'offsetof\(GG, J\.ksimd\[LJ_KSIMD_NEG\]\)']),
    ('@abs', 'local abs = math.abs local function f(a) local x = abs(a) '
     'return x end CALL(1)',
     [r'num FLOAD .* ref: nil  lit: '
      r'offsetof\(GG, J\.ksimd\[LJ_KSIMD_ABS\]\)']),
    ('@table', 'local function f(t) t.a = nil t.b = 1 return t end '
     'CALL({a = 1})', [
        r'int FLOAD .* tab\.hmask',
        r'p64 FLOAD .* tab\.node',
        r'p64 HREFK .* string "a" @ ' + RX_ADDR + r' KSLOT: @\d',
        r'p64 HREF .* string "b" @ ' + RX_ADDR,
        r'p64 EQ .* \[g->nilnode\]']),
    ('@uref', 'local uv = 0 local function f(a) uv = a return uv end '
     'CALL(1)', [r'UREFO .* lit: #0']),
    ('@call', 'local function f(a, b) return a < b, ffi.errno() end '
     'CALL("abc", "abd")', [
        r'int CALLN .* lj_str_cmp\(\{\d{4}\}, \{\d{4}\}\)',
        r'int CALLS .* lj_vm_errno\(\)']),
    ('@ctype', 'local function f() local t = ffi.C.dbg_printf("") return t '
     'end CALL()', [
        r'int CALLXS .* \[' + RX_ADDR + r'\]\(.*\) '
        r'ctype: \[\d+\] <int \(\)>']),
] + [
    ('@fpm_' + op, 'local function f(a) local x = {} return x end '
     'CALL(1.1)'.format(expr),
     [r'num FPMATH .* ref: \d{4} lit: ' + op])
    for op, expr in (('floor', 'math.floor(a)'), ('ceil', 'math.ceil(a)'),
                     ('sqrt', 'math.sqrt(a)'), ('log', 'math.log(a)'),
                     ('log2', 'math.log(a, 3)'))
]
if H.IS_GC64:
    JIT_CASES += [
        ('@basemt', 'local function f(a) local x = a.sub(1, 2) return x end '
         'CALL("12")', [
             r'tab FLOAD .* ref: nil  lit: '
             r'offsetof\(GG, g\.gcroot\[GCROOT_BASEMT_STR\]\.gcptr64\)']),
        ('@io', 'local flush = io.flush local function f() flush() end '
         'CALL()', [
             r'udt FLOAD .* ref: nil  lit: '
             r'offsetof\(GG, g\.gcroot\[GCROOT_IO_OUTPUT\]\.gcptr64\)']),
    ]


def jit_case_code(code):
    return re.sub(r'CALL\((.*)\)$',
                  lambda m: ' '.join(['f({})'.format(m.group(1))] * 4), code)


# Tarantool compiles traces at startup and the numbers do not restart
# at 1: the commands go over every slot, jit.dump tells which trace is
# which.  Setting hotloop resets the hot counters (lib_jit.c), before
# each case: whatever ran before must not have made it hot already.
NTRACES = 64
TRACES = Scenario('traces', '''
local ffi = require('ffi')
local dump = require('jit.dump')
ffi.cdef[[
  int dbg_getpid2(int, int) asm("getpid");
  int dbg_printf(const char *fmt, ...) asm("printf");
]]
jit.flush()
dump.on('tisrT', 'ir.txt')
''' + ''.join("do jit.opt.start('hotloop=1') {} end -- {}\n".format(
    jit_case_code(code), marker) for marker, code, _ in JIT_CASES) + '''
dump.off()
print()
''', ['break lj_cf_print'], [])
for n in range(1, NTRACES + 1):
    tr = '{}->J.trace[{}]'.format(GG(), n)
    cond = '{}->J.sizetrace > {} && $dbg_gcref({}) != 0'.format(GG(), n, tr)
    TRACES.cmds += [
        ('trace%d' % n, 'lj-trace (GCtrace *)$dbg_gcref({})'.format(tr),
         cond),
        ('rs%d' % n, 'lj-trace /rs (GCtrace *)$dbg_gcref({})'.format(tr),
         cond),
        ('pt%d' % n, 'eval:(long)$dbg_gcref(((GCtrace *)$dbg_gcref({}))'
         '->startpt)'.format(tr), cond),
    ]


def snap_refs(s):
    '''The IR refs and the empty slots of a snapshot, constants left out:
    their formats differ.'''
    return re.findall(r'(?<![\w+.:#@-])(\d{4}(?:/\d{4})?|----)(?![\w.])', s)


def parse_jitdump(text):
    '''jit.dump with tisr: the traces that stopped (an aborted one gives
    its number back), {traceno: {'line': start line, 'ir': {num:
    (sink/guard, phi, type, op, reg)}, 'snaps': [[ref tokens]]}}.'''
    traces, cur = {}, None
    for s in text.split('\n'):
        m = re.match(r'^---- TRACE (\d+) start .*:(\d+)$', s)
        if m:
            cur = {'line': int(m.group(2)), 'ir': {}, 'snaps': []}
            traces[int(m.group(1))] = cur
            continue
        m = re.match(r'^---- TRACE (\d+) abort', s)
        if m:
            traces.pop(int(m.group(1)), None)
            cur = None
            continue
        if re.match(r'^---- TRACE (\d+) stop', s):
            cur = None
            continue
        if cur is None:
            continue
        m = re.match(r'^\.\.\.\.\s+SNAP\s+#\d+\s+\[ (.*) \]$', s)
        if m:
            # Constants: strings, [addresses], signed numbers.
            cur['snaps'].append(snap_refs(re.sub(
                r'"[^"]*"~?|\[[^\]]*\]', 'K', m.group(1))))
            continue
        m = re.match(r'^(\d{4}) ------------ LOOP', s)
        if m:
            cur['ir'][int(m.group(1))] = (' ', ' ', '---', 'LOOP', '')
            continue
        m = re.match(r'^(\d{4}) (.{6})(.)(.) (.{3}) (\w+)', s)
        if m:
            reg = m.group(2).strip()
            cur['ir'][int(m.group(1))] = (m.group(3), m.group(4),
                                          m.group(5), m.group(6), reg)
    return traces


def parse_ljtrace(text):
    '''lj-trace /rs: {'ir': {num: (sink, guard, phi, type, op, reg)},
    'snaps': [[ref tokens]], 'proto': addr}.'''
    tr = {'ir': {}, 'snaps': [], 'lines': {}}
    m = re.search(r'proto: (0x[0-9a-f]+)', text)
    tr['proto'] = hexint(m.group(1)) if m else None
    for s in text.split('\n'):
        m = re.match(r'^\.\.\.\.\s+SNAP\s+#\d+\s+\[ (.*) \]$', s)
        if m:
            # Constants are {...}.
            tr['snaps'].append(snap_refs(re.sub(r'\{[^}]*\}', 'K',
                                                m.group(1))))
            continue
        m = re.match(r'^(\d{4}) (.{7})?(.)(.)(.)(.) (.{3}) (\w+)\s+\[', s)
        if m:
            tr['ir'][int(m.group(1))] = (
                m.group(3), m.group(5), m.group(6), m.group(7), m.group(8),
                (m.group(2) or '').strip())
            tr['lines'][int(m.group(1))] = s
    return tr


class TestTrace(DbgCase):
    SCENARIOS = (TRACES,)

    def traces(self):
        '''{marker: (traceno, jit.dump trace)}.'''
        dumped = parse_jitdump(TRACES.read('ir.txt'))
        by_line = {}
        for n, t in sorted(dumped.items()):
            by_line.setdefault(t['line'], (n, t))
        out = {}
        for marker, _, _ in JIT_CASES:
            line = TRACES.line('-- ' + marker)
            self.assertIn(line, by_line, '{}: no trace'.format(marker))
            out[marker] = by_line[line]
        return out

    def test_ir_vs_jit_dump(self):
        for marker, (n, want) in self.traces().items():
            got = parse_ljtrace(self.out(TRACES, 'trace%d' % n))
            self.assertTrue(want['ir'], marker)
            for num, (sg, phi, tp, op, _) in want['ir'].items():
                self.assertIn(num, got['ir'], '{} {:04d}'.format(marker, num))
                gsink, gguard, gphi, gtp, gop, _ = got['ir'][num]
                where = '{} {}'.format(marker, got['lines'][num])
                self.assertEqual((gtp, gop), (tp, op), where)
                if op == 'LOOP':
                    continue        # jit.dump shows no flags there.
                self.assertEqual(gphi, phi, where)
                if sg == '}':
                    self.assertEqual(gsink, '}', where)
                else:
                    self.assertEqual(gguard, sg, where)
        self.assertNoPyError(TRACES)

    def test_regs_snaps_vs_jit_dump(self):
        for marker, (n, want) in self.traces().items():
            got = parse_ljtrace(self.out(TRACES, 'rs%d' % n))
            for num, w in want['ir'].items():
                reg = got['ir'][num][5]
                wreg = w[4]
                if wreg.startswith('['):        # A spill slot.
                    wreg = '[{:#05x}]'.format(int(wreg[1:-1], 16))
                elif wreg.startswith('{'):
                    wreg = wreg.replace(' ', '')
                self.assertEqual(reg, wreg, '{} {:04d}'.format(marker, num))
            self.assertEqual(got['snaps'], want['snaps'], marker)

    def test_patterns(self):
        for marker, code, rxs in JIT_CASES:
            n = self.traces()[marker][0]
            text = self.out(TRACES, 'trace%d' % n)
            self.assertRegex(text, r'^Trace {} start\n\tproto: {:#x}\n'
                             r'\tBC: 0x[0-9a-f]+\n---- TRACE IR\n'.format(
                                 n, self.value(TRACES, 'pt%d' % n)))
            for rx in rxs:
                self.assertRegex(text, rx, marker)


JS = GG('tarantool_L') + '->J'
TRACE_STOP = Scenario('trace_stop', '''
print('go')
jit.flush()
jit.opt.start('hotloop=1')
local function trace(x)
   return x + 42, x + 0.5, x .. "1"
end
trace(1)
trace(1)
trace(1)
trace(1)
''', ['break lj_cf_print'], [
    ('state', 'lj-state'),
    ('jslots', 'lj-jslots'),
    ('jslots.n', 'eval:{0}.baseslot + {0}.maxslot'.format(JS)),
    ('cur', 'lj-trace &{}.cur'.format(JS)),
], setup=['delete',
          # The first trace_stop of the script's own function.
          'break trace_stop if $_streq((char *)(((GCstr *)$dbg_gcref(('
          '(GCproto *)$dbg_gcref({}.cur.startpt))->chunkname)) + 1), '
          '"@@SCRIPT@")'.format(JS),
          'continue'])
for k in range(40):
    TRACE_STOP.cmds.append((
        'ir%d' % k, 'lj-ir &{}.cur.ir[0x8000 - {}]'.format(JS, k),
        '$dbg_ir_isins(&{}.cur, 0x8000 - {})'.format(JS, k)))


class TestTraceStop(DbgCase):
    SCENARIOS = (TRACE_STOP,)

    def test_state(self):
        # lj_trace.c: trace_stop() is called in LJ_TRACE_ASM, after the
        # assembler.
        lines = self.out(TRACE_STOP, 'state').rstrip('\n').split('\n')
        self.assertEqual(lines[0], 'VM state: ASM')
        self.assertRegex(lines[1], r'^GC state: [A-Z]+$')
        self.assertEqual(lines[2], 'JIT state: ASM')

    def test_ir_const(self):
        # The constants of the trace, REF_BIAS down to nk.  Tarantool
        # adds its own to every trace, so not at fixed refs as in the
        # LuaJIT test: each one is somewhere.
        p = 'p64' if H.IS_GC64 else 'p32'
        outs = {}
        for k in range(40):
            r = self.raw(TRACE_STOP, 'ir%d' % k)
            if not r.get('skipped'):
                outs[k] = self.out(TRACE_STOP, 'ir%d' % k)
        self.assertRegex(outs[0], p + ' BASE')
        self.assertRegex(outs[1], 'nil KPRI')
        self.assertRegex(outs[2], 'fal KPRI')
        self.assertRegex(outs[3], 'tru KPRI')
        text = ''.join(outs[k] for k in sorted(outs))
        for rx in (r'int KINT .*cst: integer 42\s*\n',
                   r'num KNUM .*cst: number 0.5\s*\n',
                   r'str KGC .*cst: string "1"',
                   p + r' KPTR .*cst: \[' + RX_ADDR + r'\]'):
            self.assertRegex(text, rx)
        self.assertNoPyError(TRACE_STOP)

    def test_jslots(self):
        lines = self.out(TRACE_STOP, 'jslots').rstrip('\n').split('\n')
        n = self.value(TRACE_STOP, 'jslots.n')
        first = 1 + (1 if H.IS_GC64 else 0)
        self.assertEqual(len(lines), n - first)
        nums = []
        for s in lines:
            m = re.match(r'^0x[0-9a-f]+ (\d{4}) [B ] \[[F ][C ]\] (\w{3}|   ) '
                         r'0x[0-9a-f]+', s)
            self.assertIsNotNone(m, s)
            nums.append(int(m.group(1)))
        self.assertEqual(nums, list(range(n - 1, first - 1, -1)))

    def test_cur(self):
        self.assertRegex(self.out(TRACE_STOP, 'cur'),
                         r'^Trace \d+ start\n\tproto: ' + RX_ADDR)


# The GC states: the stop is in the function of each state, in the full
# cycle of the script (the GC of the startup may run before tarantool_L
# is set).
GC_SCRIPT = '''
print('go')
local obj = newproxy(true)
getmetatable(obj).__gc = function() end
obj = nil
collectgarbage()
'''
GC_STATES = [
    # The plain names: LuaJIT is one amalgamated unit, "lj_gc.c:atomic"
    # is not found.
    ('PAUSE', 'break gc_mark_start'),
    ('PROPAGATE', 'break propagatemark'),
    ('ATOMIC', 'break atomic'),
    ('SWEEPSTRING', 'break gc_sweep_str_chain'),
    ('SWEEP', 'break gc_sweep if g->gc.state == 4'),
    ('FINALIZE', 'break gc_finalize'),
]
GC_SCENARIOS = [Scenario('gc_' + st.lower(), GC_SCRIPT, ['break lj_cf_print'],
                         gc_cmds('tarantool_L'),
                         setup=['delete', brk, 'continue'])
                for st, brk in GC_STATES]


class TestGCStates(DbgCase):
    SCENARIOS = GC_SCENARIOS

    def test_states(self):
        for (state, _), scen in zip(GC_STATES, GC_SCENARIOS):
            with self.subTest(state=state):
                lines = self.out(scen, 'state').rstrip('\n').split('\n')
                self.assertEqual(lines[:2], ['VM state: GC',
                                             'GC state: ' + state])
                self.assertEqual(lines[2], 'JIT state: IDLE')
                check_gc(self, scen, state)
                self.assertNoPyError(scen)


# Enums missing from the debug info ################################

# The max members of the enums of luajit_dbg.py, and the two members
# that tell LJ_64 and LJ_GC64.
ENUM_MAXES = ['BCM_max', 'BC__MAX', 'FF__MAX', 'MM__MAX', 'LJ_VMST__MAX',
              'IR__MAX', 'IRT__MAX', 'IRFL__MAX', 'IRFPM__MAX',
              'IRCALL__MAX']
HIDDEN = ENUM_MAXES + ['IRT_PTR', 'IRT_PGC']


def noenum(scen):
    '''The commands of scen, then once more in the same process with the
    enums hidden (hide_enums.py) and the extension sourced again: the
    outputs must be the same.'''
    cmds = list(scen.cmds) + [('hide', 'source ' + H.HIDE_ENUMS),
                              ('reload', 'source ' + H.LUAJIT_DBG)]
    cmds += [('noenum.' + e[0],) + tuple(e[1:]) for e in scen.cmds
             if not e[1].startswith('eval:')]
    return scen.variant(scen.name + '_noenum', cmds=cmds,
                        gdb_env={'DBG_HIDE_ENUMS': ','.join(HIDDEN)})


NOENUM = [noenum(s) for s in (BASIC, VALUES, PROTO, TRACES, TRACE_STOP)]
ENUMS = Scenario('enums', 'print(1)\n', ['break lj_cf_print'],
                 [('check', 'dbg-enum-check')],
                 sources=[H.LUAJIT_DBG, H.ENUMCHECK])
FALLBACK_NOTE = re.compile(r'^luajit_dbg\.py: failed to resolve .* enum '
                           r'members using (\w+), taking the built-in list\n',
                           re.M)


class TestEnumFallback(DbgCase):
    SCENARIOS = [ENUMS] + NOENUM

    def test_tables_match_dwarf(self):
        # The built-in members are those of the debug info of every
        # version tested.
        import json
        got = json.loads(self.out(ENUMS, 'check'))
        self.assertEqual(sorted(got), sorted(ENUM_MAXES))
        for name, v in sorted(got.items()):
            self.assertEqual(v['fallback'], v['dwarf'], name)

    def test_without_enums(self):
        # tarantool/tarantool#13094: with none of its enums in the debug
        # info the extension loads and prints what it prints with them.
        used = set()
        for scen in NOENUM:
            with self.subTest(scenario=scen.name):
                reload = self.out(scen, 'reload')
                self.assertNotIn('failed to load', reload)
                TestLoad.check_load(self, reload)
                for e in scen.cmds:
                    key = e[0]
                    if not key.startswith('noenum.'):
                        continue
                    for mode in scen.modes():
                        a = self.raw(scen, key[7:], mode)
                        b = self.raw(scen, key, mode)
                        self.assertEqual(bool(a.get('skipped')),
                                         bool(b.get('skipped')), key)
                        used.update(FALLBACK_NOTE.findall(b['out'] or ''))
                        self.assertEqual(
                            FALLBACK_NOTE.sub('', b['out'] or ''),
                            a['out'] or '', '{} ({})'.format(key, mode))
                        self.assertEqual(b['err'], a['err'], key)
                used.update(FALLBACK_NOTE.findall(reload))
                self.assertNoPyError(scen)
        # Every table was taken.
        self.assertEqual(sorted(used), sorted(ENUM_MAXES))


if __name__ == '__main__':
    unittest.main(verbosity=2)
