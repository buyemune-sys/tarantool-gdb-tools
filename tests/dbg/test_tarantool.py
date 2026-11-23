# Tests of tarantool-gdb.py against tarantool: every command and
# pretty-printer, live and on a core, the output compared with what the
# Lua side or gdb itself says.
#
# One instance (box.cfg{}, no listen, memtx with MVCC, a WAL in the
# work directory under TMPDIR) builds the objects: a space and a tuple,
# fibers (a sleeper, one with an open transaction), MsgPack written by
# tarantool's own encoders, decimal/uuid/datetime cdata.  It writes what
# it knows to oracle.json and passes the objects to print(), so that at
# the stop in lj_cf_print they are the arguments of the C function:
# L->base[i].

import os
import re
import sys
import unittest

import harness as H
from harness import DbgCase, Scenario, gcval

ORACLE_TT = os.path.join(H.HERE, 'oracle_tt.py')
SOURCES = [H.TT_GDB, ORACLE_TT]
TLS_BASE = '$tpidr_el0' if H.ARCH == 'arm64' else '$fs_base'

COMMANDS = ['info tt-fibers', 'tt-fiber', 'tt-list', 'tt-list-walk',
            'tt-mp']
FUNCTIONS = ['find', 'tl_var_addr', 'tl_var_find_offset', 'tl_var_like']


def arg(i):
    return '(L->base + {})'.format(i)


def cdata(i, tp):
    '''The payload of the cdata argument i, as a pointer to tp.'''
    return '(({} *)((char *){} + sizeof(GCcdata)))'.format(tp, gcval(arg(i)))


def strdata(i):
    '''The bytes of the string argument i.'''
    return '((char *){} + sizeof(GCstr))'.format(gcval(arg(i)))


# MsgPack samples: (name, Lua expression of the encoded string).  Raw
# bytes are for what tarantool does not encode from Lua.
MP_SAMPLES = [
    ('scalars', "msgpack.encode({1, -2, 3.5, 'str', true, false, box.NULL,"
                " 300, -300, 2^40})"),
    ('nested', "msgpack.encode({k = {1, {x = 'y'}}})"),
    ('empty', "msgpack.encode({{}, setmetatable({}, {__serialize = 'map'})})"),
    ('bin', "'\\xc4\\x03abc'"),
    ('decimal', 'msgpack.encode(dec)'),
    ('decimal_int', "msgpack.encode(decimal.new('123456789012345'))"),
    ('decimal_long', 'msgpack.encode(dec_long)'),
    ('uuid', 'msgpack.encode(u)'),
    ('datetime', 'msgpack.encode(dt)'),
    ('datetime_tz', 'msgpack.encode(dt_tz)'),
    ('interval', 'msgpack.encode(iv)'),
    ('error', 'msgpack.encode(err)'),
    # MP_TUPLE (7): format id 5, payload {1, 'x'}.
    ('tuple', "'\\xc7\\x05\\x07\\x05\\x92\\x01\\xa1x'"),
    # An extension type tarantool does not have, in an array.
    ('unknown_ext', "'\\x92\\xd4\\x63\\x2a\\x01'"),
]
MP = dict((name, i) for i, (name, _) in enumerate(MP_SAMPLES))
# The print() arguments: the tuple, the cdata, then the MsgPack strings.
ARGS = ['t', 'dec', 'u', 'dt'] + [e for _, e in MP_SAMPLES]
MP_BASE = 4

BOX_LUA = '''
local ffi = require('ffi')
local fiber = require('fiber')
local msgpack = require('msgpack')
local decimal = require('decimal')
local uuid = require('uuid')
local datetime = require('datetime')

box.cfg{log = 'tarantool.log', memtx_use_mvcc_engine = true,
        strip_core = false}
local s = box.schema.space.create('test', {format = {
    {'id', 'unsigned'}, {'name', 'string'},
    {'data', 'any', is_nullable = true}}})
s:create_index('pk')
local t = s:insert{1, 'one', {a = 1}}
ORACLE.tuple = t:totable()
ORACLE.format = s:format()

local cond = fiber.cond()
local sleeper = fiber.new(function() fiber.sleep(1000) end)
sleeper:name('tt-sleeper')
local txner = fiber.new(function()
    box.begin()
    s:replace{2, 'two'}
    ORACLE.txn_id = box.txn_id()
    cond:wait()
end)
txner:name('tt-txn')
fiber.yield()
ORACLE.fibers = {}
for fid, f in pairs(fiber.info()) do
    ORACLE.fibers[tostring(fid)] = f.name
end
ORACLE.running = fiber.self():id()
ORACLE.running_name = fiber.self():name()
ORACLE.sleeper = sleeper:id()
ORACLE.txner = txner:id()

local dec = decimal.new('-12345.678')
-- As many digits as the build has: 38, 76 since 3.5.
local dec_long = decimal.new(string.rep('7', 36) .. '.25')
local ok, d76 = pcall(decimal.new, string.rep('7', 70) .. '.123456')
if ok and #tostring(d76) > 60 then
    dec_long = d76
end
ORACLE.dec_long = tostring(dec_long)
local u = uuid.fromstr('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
local dt = datetime.new{year = 2024, month = 2, day = 29, hour = 13,
                        min = 14, sec = 15, nsec = 123000000,
                        tzoffset = 180}
local dt_tz = datetime.new{year = 1999, month = 12, day = 31, hour = 23,
                           tz = 'Europe/Moscow'}
local iv = datetime.interval.new{year = 1, month = -2, day = 3}
local err = box.error.new{code = 42, reason = 'boom'}
ORACLE.dec = tostring(dec)
ORACLE.uuid = tostring(u)
ORACLE.dt = tostring(dt)
ORACLE.dt_tz = tostring(dt_tz)
ORACLE.err = err:unpack()
oracle_write()
print(@ARGS@) -- the stop
os.exit(0)
'''.replace('@ARGS@', ', '.join(ARGS))

# The pointers the commands work on.
BOX_PREP = [
    'set $t = *(struct tuple **){}'.format(cdata(0, 'char')),
    'set $dec = {}'.format(cdata(1, 'decimal_t')),
    'set $u = {}'.format(cdata(2, 'struct tt_uuid')),
    'set $dt = {}'.format(cdata(3, 'struct datetime')),
] + ['set $mp_{} = {}'.format(name, strdata(MP_BASE + i))
     for i, (name, _) in enumerate(MP_SAMPLES)]

if H.VERSION_TUPLE >= (3, 4):
    TXNS = 'txns'
    TXN_FIRST = '((struct txn *)((char *)txns.next - ' \
        '(long)&((struct txn *)0)->in_txns))'
else:
    TXNS = 'txm.all_txs'
    TXN_FIRST = '((struct txn *)((char *)txm.all_txs.next - ' \
        '(long)&((struct txn *)0)->in_all_txs))'

BOX_PREP.append('set $rl = &{}->on_commit'.format(TXN_FIRST))

QUORUM = 'replicaset_on_quorum_change' if H.VERSION_TUPLE >= (3, 6) \
    else 'replicaset_on_quorum_gain'
# The entries of the list table that resolve: symbols and entry infos.
LUT_DUMP = ("python RlistLut._init(); StailqLut._init(); "
            "gdb.write('\\n'.join(sorted(set(list(RlistLut._symbols_map) + "
            "[str(e) for lut in (RlistLut, StailqLut) "
            "for m in lut._containers_map.values() for e in m.values()]))) "
            "+ '\\n')")

WALK = 40       # more tt-list-walk steps than there are fibers

BOX_CMDS = [
    ('fibers', 'info tt-fibers'),
    ('fiber', 'tt-fiber'),
    ('fiber.bad', 'tt-fiber not-a-number'),
    ('fiber.unknown', 'tt-fiber 4294967295'),
    ('tuple', 'print *$t'),
    ('tuple.format_id', 'eval:$t->format_id'),
    ('tuple.flags', 'eval:$t->flags'),
    ('tuple.refs', 'eval:$t->local_refs'),
    ('alive', 'tt-list -fields fid,name -- main_cord.alive'),
    ('alive.len', 'eval:$dbg_rlist_len(&main_cord.alive)'),
    ('alive.print', 'print main_cord.alive'),
    ('alive.item', 'print *main_cord.alive.next'),
    ('alive.next', 'eval:(long)main_cord.alive.next'),
    ('alive.rev', 'tt-list -reverse -fields fid -- main_cord.alive'),
    ('alive.entry_info',
     'tt-list -entry-info fiber::link -fields fid -- main_cord.alive'),
    ('alive.pred', 'tt-list -fields fid,name -predicate '
     '\'$_streq($entry->name, "tt-sleeper")\' -- main_cord.alive'),
    ('entry.show', 'show print tt-list-entry'),
    ('entry.off', 'set print tt-list-entry off'),
    ('alive.raw', 'print main_cord.alive'),
    ('entry.on', 'set print tt-list-entry on'),
    ('txns', 'tt-list -fields id -- ' + TXNS),
    # A list in a heap object: its head says nothing of the entries.
    ('stmts', 'tt-list -entry-info txn_stmt::next -- {}->stmts'.format(
        TXN_FIRST)),
    ('stmts.len', 'eval:$dbg_stailq_len(&{}->stmts)'.format(TXN_FIRST)),
    # A list the table cannot tell, a broken -entry-info, and a list
    # printed after them.
    ('list.unknown', 'tt-list -- *$rl'),
    ('list.bad_entry', 'tt-list -entry-info no_such_type::link -- '
     'main_cord.alive'),
    ('list.after', 'print main_cord.alive'),
    ('quorum', 'tt-list -fields run -- ' + QUORUM),
    ('quorum.len', 'eval:$dbg_rlist_len(&{})'.format(QUORUM)),
    ('lut', LUT_DUMP),
    ('txn_cache', 'print txn_cache'),
    ('txn_cache.len', 'eval:$dbg_stailq_len(&txn_cache)'),
    ('token', 'print tuple_formats[$t->format_id]->fields.root'),
    ('dec', 'print *$dec'),
    ('uuid', 'print *$u'),
    ('dt', 'print *$dt'),
] + [('mp.' + name, 'tt-mp $mp_' + name) for name, _ in MP_SAMPLES] + [
    ('walk.{}'.format(i), 'tt-list-walk -fields fid -- main_cord.alive')
    for i in range(WALK)
] + [
    # The thread-local functions; gdb's own TLS lookup (&cord_ptr) is
    # the oracle, read live only.
    ('tl.offset', 'print $tl_var_find_offset(&main_cord)'),
    ('tl.offset.gdb', 'eval:(long)&cord_ptr - (long){}'.format(TLS_BASE)),
    ('tl.like', 'print $tl_var_like(&main_cord)'),
    ('tl.addr', 'print $tl_var_addr($tl_var_find_offset(&main_cord))'),
    ('tl.addr.gdb', 'eval:(long)&cord_ptr'),
    ('tl.short', 'print $tl_var_find_offset(&main_cord, 7)'),
    ('tl.negative', 'print $tl_var_find_offset(&main_cord, -1)'),
    ('tl.show', 'show tl-var-search-range'),
    ('tl.threads', 'dbg-threads output $tl_var_like(&main_cord)'),
    ('tl.threads.gdb', 'dbg-threads output cord_ptr'),
    # The first word at the thread pointer (the TCB): is the TLS of the
    # thread in the core?
    ('threads.tp', 'dbg-threads output *(long *){}'.format(TLS_BASE)),
    ('find', 'print $find((char *)&main_cord.fiber, 8, main_cord.fiber)'),
    ('find.gdb', 'eval:(long)&main_cord.fiber'),
    # Every thread: its cord and fibers.  The gdb side (cord_ptr) is
    # read live only: a core may have no libthread_db.
    ('threads.fibers', 'dbg-threads info tt-fibers'),
    ('threads.cord', 'dbg-threads output cord_ptr->name'),
    ('threads.alive', 'dbg-threads output $dbg_rlist_len(&cord_ptr->alive)'),
    ('threads.tuple', 'dbg-threads output *$t'),
    # The fiber switch, last: it changes what the commands see.
    ('sleeper', 'eval "tt-fiber %d", $dbg_fiber_id("tt-sleeper")'),
    ('sleeper.fibers', 'info tt-fibers'),
    ('sleeper.current', 'tt-fiber'),
    ('sleeper.bt', 'bt'),
    ('txner', 'eval "tt-fiber %d", $dbg_fiber_id("tt-txn")'),
    ('txner.bt', 'bt'),
    ('back', 'eval "tt-fiber %d", main_cord.fiber->fid'),
    ('back.fibers', 'info tt-fibers'),
    ('back.bt', 'bt'),
    # The selection survives a flush of gdb's frame cache.
    ('flush', 'eval "tt-fiber %d", $dbg_fiber_id("tt-sleeper")'),
    ('flush.bt', 'bt'),
    ('flush.cmd', 'maint flush register-cache'),
    ('flush.bt2', 'bt'),
    # Another thread while a fiber is selected, and back.
    ('thread.2', 'thread 2'),
    ('thread.2.bt', 'bt 1'),
    ('thread.1', 'thread 1'),
    ('thread.1.bt', 'bt'),
    ('flush.back', 'eval "tt-fiber %d", main_cord.fiber->fid'),
] + [
    # What lj_bt.py uses: fiber(), Cord().fibers(), FiberUnwinder's
    # instance(), fiber(), set_fiber(), reset_fiber().
    ('ljbt.running', "python gdb.write('%d\\n' % int(fiber()['fid']))"),
    ('ljbt.set', "python u = FiberUnwinder.instance(); "
     "u.set_fiber([f for f in Cord().fibers() "
     "if f['name'].string() == 'tt-sleeper'][0]); "
     "gdb.write('%d %d\\n' % (int(u.fiber()['fid']), int(fiber()['fid'])))"),
    ('ljbt.set.bt', 'bt'),
    ('ljbt.reset', "python u = FiberUnwinder.instance(); u.reset_fiber(); "
     "gdb.write('%d\\n' % int(u.fiber()['fid']))"),
    ('ljbt.reset.bt', 'bt'),
    ('ljbt.setrun', "python u = FiberUnwinder.instance(); "
     "u.set_fiber(fiber()); gdb.write('%d\\n' % int(u.fiber()['fid']))"),
    ('ljbt.setrun.bt', 'bt'),
]

BOX = Scenario('box', BOX_LUA, ['break lj_cf_print'], BOX_CMDS,
               prep=BOX_PREP, sources=SOURCES)

# The same with gdb unable to read the thread-local variables (a core
# with no libthread_db and a gdb before 16): the extension reads them at
# their offset from the thread pointer.
NOTLS_HIDDEN = 'cord_ptr,&cord_ptr,tuple_formats,&tuple_formats'
NOTLS = BOX.variant('box-notls', sources=[H.HIDE_ENUMS] + SOURCES,
                    gdb_env={'DBG_HIDE_ENUMS': NOTLS_HIDDEN}, cmds=[
                        ('fibers', 'info tt-fibers'),
                        ('fiber', 'tt-fiber'),
                        ('tuple', 'print *$t'),
                        ('threads.fibers', 'dbg-threads info tt-fibers'),
                        ('threads.tp', 'dbg-threads output *(long *){}'
                         .format(TLS_BASE)),
                    ])


def mp_text(v):
    '''A JSON value of the oracle as tt-mp prints its MsgPack.'''
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return '{}U'.format(v) if v >= 0 else str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return '"{}"'.format(v)
    if isinstance(v, list):
        return '{' + ', '.join(mp_text(x) for x in v) + '}'
    return '{' + ', '.join('[{}] = {}'.format(mp_text(k), mp_text(x))
                           for k, x in v.items()) + '}'


def value(out):
    '''The value of a `print`: the text after "$N = ".'''
    m = re.match(r'\$\d+ = (.*)', out, re.S)
    assert m, out
    return m.group(1).strip()


RX_FIBER_ROW = re.compile(r'(?P<run>\*?)\s+(?P<fid>\d+)\s+TtFiber\s+'
                          r'"(?P<name>.*)"\s+(?P<unw>\*?)\s+0x[0-9a-f]+')
RX_ENTRY = re.compile(r'\[(\d+)\] \(\((\w+)\*\)(0x[0-9a-f]+)\) = \{(.*?)\}'
                      r'(?=, \[\d+\] |\}$)')


def fiber_rows(out):
    lines = out.strip().split('\n')
    rows = []
    for s in lines[1:]:
        m = RX_FIBER_ROW.match(s)
        assert m, s
        rows.append((m.group('run'), int(m.group('fid')), m.group('name'),
                     m.group('unw')))
    return rows


def entries(out):
    '''[(index, type, address, fields text)] of a tt-list output.'''
    return [(int(i), tp, int(a, 16), f)
            for i, tp, a, f in RX_ENTRY.findall(out.strip())]


def fids(out):
    return [int(re.search(r'fid = (\d+)', f).group(1))
            for _, _, _, f in entries(out)]


class TestLoad(DbgCase):
    SCENARIOS = (BOX,)

    def test_load(self):
        # Sourced after `file`, before `run` or `core-file`: what a
        # .gdbinit does.
        for mode in BOX.modes():
            res = self.result(BOX, mode)
            self.assertEqual(res['load_err'], [], mode)
            self.assertNotIn('Traceback', res['load'], mode)
        self.assertNoPyError(BOX)


class TestFibers(DbgCase):
    SCENARIOS = (BOX,)

    def test_info(self):
        o = BOX.oracle()
        rows = fiber_rows(self.out(BOX, 'fibers'))
        got = dict((fid, name) for _, fid, name, _ in rows)
        # sched is fiber 1 of every cord; fiber.info() does not list it.
        self.assertEqual(got.pop(1), 'sched')
        self.assertEqual(got, dict((int(k), v)
                                   for k, v in o['fibers'].items()))
        self.assertEqual([fid for run, fid, _, _ in rows if run],
                         [o['running']])
        self.assertEqual([fid for _, fid, _, unw in rows if unw],
                         [o['running']])

    def test_current(self):
        o = BOX.oracle()
        self.assertRegex(self.out(BOX, 'fiber'),
                         r'^Current fiber is {} "{}" 0x'.format(
                             o['running'], re.escape(o['running_name'])))

    def test_bad_id(self):
        for mode, err in self.errs(BOX, 'fiber.bad'):
            self.assertEqual(err, 'Invalid fiber ID: not-a-number', mode)
        for mode, err in self.errs(BOX, 'fiber.unknown'):
            self.assertEqual(err, 'Unknown fiber 4294967295.', mode)

    def frames(self, key):
        '''[(level, function)] of a bt.'''
        return [(int(n), f) for n, f in re.findall(
            r'^#(\d+) +(?:0x[0-9a-f]+ in )?([\w:<>~]+)',
            self.out(BOX, key), re.M)]

    def test_switch(self):
        o = BOX.oracle()
        self.assertEqual(self.out(BOX, 'sleeper').strip().split('\n')[0],
                         'Switching stack unwinder to fiber {} (0x{:x} '
                         '(tt-sleeper))'.format(o['sleeper'],
                                                self.fiber_addr('sleeper')))
        rows = fiber_rows(self.out(BOX, 'sleeper.fibers'))
        self.assertEqual([fid for run, fid, _, _ in rows if run],
                         [o['running']])
        self.assertEqual([fid for _, fid, _, unw in rows if unw],
                         [o['sleeper']])
        self.assertRegex(self.out(BOX, 'sleeper.current'),
                         r'^Current fiber is {} "tt-sleeper" '.format(
                             o['sleeper']))

    def fiber_addr(self, which):
        out = self.out(BOX, 'alive')
        fid = BOX.oracle()[which]
        for _, _, addr, f in entries(out):
            if f.startswith('fid = {},'.format(fid)):
                return addr
        self.fail('no fiber {} in {}'.format(fid, out))

    def test_switch_bt(self):
        # The stack of a fiber that is not running: from the context it
        # saved when it yielded.  Frame 0 (the running fiber's) is
        # filtered out.
        for key, inner in (('sleeper.bt', 'lbox_fiber_sleep'),
                           ('txner.bt', 'fiber_cond_wait_timeout')):
            fr = self.frames(key)
            names = [f for _, f in fr]
            self.assertEqual(fr[0][0], 1, key)
            self.assertIn(inner, names, key)
            self.assertIn('lua_fiber_run_f', names, key)
            self.assertNotIn('lj_cf_print', names, key)
            self.assertLess(names.index(inner),
                            names.index('lua_fiber_run_f'), key)

    def test_switch_back(self):
        o = BOX.oracle()
        rows = fiber_rows(self.out(BOX, 'back.fibers'))
        self.assertEqual([fid for _, fid, _, unw in rows if unw],
                         [o['running']])
        fr = self.frames('back.bt')
        self.assertEqual(fr[0], (0, 'lj_cf_print'))
        self.assertIn('run_script_f', [f for _, f in fr])

    def test_flush(self):
        # The frames are unwound again after a flush of the frame cache,
        # or a thread switch and back: the selected fiber is still the
        # one shown.  In the other thread its own stack.
        for key in ('flush.bt', 'flush.bt2', 'thread.1.bt'):
            names = [f for _, f in self.frames(key)]
            self.assertIn('lbox_fiber_sleep', names, key)
            self.assertNotIn('lj_cf_print', names, key)
        self.assertEqual(self.frames('thread.2.bt')[0][0], 0)

    def test_ljbt_interface(self):
        o = BOX.oracle()
        self.assertEqual(self.out(BOX, 'ljbt.running').strip(),
                         str(o['running']))
        self.assertEqual(self.out(BOX, 'ljbt.set').strip(),
                         '{} {}'.format(o['sleeper'], o['running']))
        names = [f for _, f in self.frames('ljbt.set.bt')]
        self.assertIn('lbox_fiber_sleep', names)
        self.assertNotIn('lj_cf_print', names)
        # Back to the running fiber: reset_fiber(), or set_fiber() of it.
        for key in ('ljbt.reset', 'ljbt.setrun'):
            self.assertEqual(self.out(BOX, key).strip(), str(o['running']))
            fr = self.frames(key + '.bt')
            self.assertEqual(fr[0], (0, 'lj_cf_print'), key)
            self.assertIn('run_script_f', [f for _, f in fr], key)

    def test_threads(self):
        # In every thread the fibers of its own cord (sched, then the
        # alive ones).
        cords = per_thread(self.raw(BOX, 'threads.cord', 'live')['out'])
        alive = per_thread(self.raw(BOX, 'threads.alive', 'live')['out'])
        names = set()
        for mode, fib in thread_outs(self, BOX, 'threads.fibers'):
            if mode == 'live':
                self.assertEqual(sorted(fib), sorted(cords))
            for rank, text in fib.items():
                self.assertNotIn('!error', text, (mode, rank, text))
                rows = fiber_rows(text)
                self.assertEqual(len(rows), int(alive[rank]) + 1,
                                 (mode, rank, text))
                self.assertIn((1, 'sched'),
                              [(fid, n) for _, fid, n, _ in rows])
                names.add(re.search(r'"([^"]*)"', cords[rank]).group(1))
        self.assertIn('main', names)
        self.assertIn('wal', names)


def per_thread(text):
    '''{rank: output} of a dbg-threads command.'''
    parts = re.split(r'^### (\d+)\n', text, flags=re.M)[1:]
    return dict((int(r), s) for r, s in zip(parts[::2], parts[1::2]))


def thread_outs(case, scen, key):
    '''[(mode, {rank: output})] of a dbg-threads command, the core one
    without the threads whose TLS the core lost: gcore reads a mapping
    whole, and a thread stack with a guard region (MADV_GUARD_INSTALL,
    Linux 6.13+ with a recent glibc) cannot be read whole, so it is
    written as zeroes.  The output of a thread in the core must be the
    live one.'''
    live = per_thread(case.raw(scen, key, 'live')['out'])
    got = [('live', live)]
    if 'core' in scen.modes():
        core = per_thread(case.raw(scen, key, 'core')['out'])
        tp = dict((m, per_thread(case.raw(scen, 'threads.tp', m)['out']))
                  for m in ('live', 'core'))
        lost = [r for r in tp['live'] if tp['core'].get(r) != tp['live'][r]]
        if lost:
            sys.stderr.write('\n{}: the core has no TLS of threads {} '
                             '(gcore and guard regions), not checked\n'
                             .format(key, lost))
        core = dict((r, s) for r, s in core.items() if r not in lost)
        for r, s in core.items():
            case.assertEqual(s, live.get(r), '{} thread {}: the core output '
                             'differs'.format(key, r))
        got.append(('core', core))
    return got


class TestThreadLocal(DbgCase):
    SCENARIOS = (BOX,)

    def live(self, key):
        return self.raw(BOX, key, 'live')['out']

    def test_find_offset(self):
        # The offset of cord_ptr, the variable that holds &main_cord in
        # the main thread; nothing else printed.
        self.assertEqual(value(self.out(BOX, 'tl.offset')),
                         str(self.value(BOX, 'tl.offset.gdb')))
        self.assertRegex(self.out(BOX, 'tl.offset'), r'^\$\d+ = -?\d+\n$')

    def test_like(self):
        self.assertRegex(value(self.out(BOX, 'tl.like')),
                         r'^\(struct cord \*\) 0x[0-9a-f]+ <main_cord>$')
        # In every thread: its own cord_ptr.
        want = per_thread(self.live('tl.threads.gdb'))
        self.assertGreater(len(want), 2)
        for mode, got in thread_outs(self, BOX, 'tl.threads'):
            for rank, s in got.items():
                self.assertEqual(re.sub(r'^\(.*?\) ', '', s),
                                 re.sub(r'^\(.*?\) ', '', want[rank]),
                                 (mode, rank))

    def test_addr(self):
        self.assertEqual(value(self.out(BOX, 'tl.addr')),
                         '(void *) 0x{:x}'.format(
                             self.value(BOX, 'tl.addr.gdb')))

    def test_bad_range(self):
        for mode, err in self.errs(BOX, 'tl.short'):
            self.assertEqual(err, 'Range of size 7 is not able to contain '
                             'variable of size 8.', mode)
        for mode, err in self.errs(BOX, 'tl.negative'):
            self.assertEqual(err, 'Invalid range -1. It must be greater '
                             'than zero.', mode)
        self.assertEqual(self.out(BOX, 'tl.show').strip(),
                         'Range for searching thread-local variables is '
                         '65536')

    def test_find(self):
        self.assertEqual(value(self.out(BOX, 'find')),
                         str(self.value(BOX, 'find.gdb')))


class TestNoTLS(DbgCase):
    '''gdb cannot read cord_ptr or tuple_formats: the commands work as
    with it.'''
    SCENARIOS = (BOX, NOTLS)

    def rows(self, scen, key):
        return [(fid, name) for _, fid, name, _ in
                fiber_rows(self.out(scen, key))]

    def test_fibers(self):
        self.assertEqual(self.rows(NOTLS, 'fibers'), self.rows(BOX, 'fibers'))
        self.assertEqual(self.out(NOTLS, 'fiber'), self.out(BOX, 'fiber'))
        self.assertNoPyError(NOTLS)

    def test_threads(self):
        want = dict((r, [(f, n) for _, f, n, _ in fiber_rows(s)])
                    for r, s in per_thread(self.raw(
                        BOX, 'threads.fibers', 'live')['out']).items())
        for mode, got in thread_outs(self, NOTLS, 'threads.fibers'):
            for rank, text in got.items():
                self.assertNotIn('!error', text, (mode, rank))
                self.assertEqual([(f, n) for _, f, n, _ in fiber_rows(text)],
                                 want[rank], (mode, rank))

    def test_tuple(self):
        self.assertEqual(re.sub(r'0x[0-9a-f]+', 'X',
                                value(self.out(NOTLS, 'tuple'))),
                         re.sub(r'0x[0-9a-f]+', 'X',
                                value(self.out(BOX, 'tuple'))))

class TestTuple(DbgCase):
    SCENARIOS = (BOX,)

    def test_tuple(self):
        o = BOX.oracle()
        out = value(self.out(BOX, 'tuple'))
        self.assertIn('format_id = {},'.format(
            self.value(BOX, 'tuple.format_id')), out)
        self.assertIn('local_refs = {} '.format(
            self.value(BOX, 'tuple.refs')), out)
        self.assertRegex(out, r'flags = 0x{:02x}[ ,]'.format(
            self.value(BOX, 'tuple.flags')))
        self.assertTrue(out.endswith(
            'data = {}}}'.format(mp_text(o['tuple']))), out[-300:])

    def test_threads(self):
        # The same tuple from every thread: since 3.7 tuple_formats is
        # thread-local, NULL where there are no tuples (WAL, iproto...).
        want = value(self.out(BOX, 'tuple'))
        for mode, got in thread_outs(self, BOX, 'threads.tuple'):
            self.assertGreater(len(got), 0, mode)
            for rank, s in got.items():
                self.assertEqual(s.strip(), want, (mode, rank))

    def test_format_token(self):
        # The JSON tree of the format: one child per field of the space.
        o = BOX.oracle()
        out = value(self.out(BOX, 'token'))
        children = re.findall(r'type=\d+ id=(\d+) hash=\d+ parent=0x[0-9a-f]+'
                              r' sibling_idx=(\d+) field_id=(\d+)', out)
        self.assertEqual([(int(a), int(b), int(c)) for a, b, c in children],
                         [(i, i, i) for i in range(len(o['format']))], out)


class TestLists(DbgCase):
    SCENARIOS = (BOX,)

    def alive(self):
        out = self.out(BOX, 'alive')
        self.assertTrue(out.startswith('$'), out)
        n = self.value(BOX, 'alive.len')
        self.assertIn('rlist<fiber::link> of length {},'.format(n), out)
        got = entries(out)
        self.assertEqual([e[0] for e in got], list(range(n)))
        self.assertEqual(set(e[1] for e in got), {'fiber'})
        return got

    def test_alive(self):
        o = BOX.oracle()
        names = {}
        for _, _, _, f in self.alive():
            m = re.match(r'fid = (\d+), name = 0x[0-9a-f]+ "(.*)"$', f)
            self.assertIsNotNone(m, f)
            names[int(m.group(1))] = m.group(2)
        self.assertEqual(names, dict((int(k), v)
                                     for k, v in o['fibers'].items()))

    def test_print(self):
        # print of the head is tt-list with every field.
        out = self.out(BOX, 'alive.print')
        self.assertEqual([(i, tp, a) for i, tp, a, _ in entries(out)],
                         [(i, tp, a) for i, tp, a, _ in self.alive()])
        self.assertIn('fid = ', out)
        self.assertIn('stack_size = ', out)

    def test_item(self):
        # An item, not the head: that one entry, with its index.
        first = self.alive()[0]
        out = self.out(BOX, 'alive.item')
        self.assertRegex(out, r'\[0\] \(\(fiber\*\)0x{:x}\) = \{{'.format(
            first[2]))

    def test_reverse(self):
        fwd = [int(re.match(r'fid = (\d+)', f).group(1))
               for _, _, _, f in self.alive()]
        self.assertEqual(fids(self.out(BOX, 'alive.rev')), fwd[::-1])

    def test_entry_info(self):
        self.assertEqual(fids(self.out(BOX, 'alive.entry_info')),
                         fids(self.out(BOX, 'alive')))

    def test_predicate(self):
        o = BOX.oracle()
        got = entries(self.out(BOX, 'alive.pred'))
        self.assertEqual(len(got), 1, got)
        self.assertRegex(got[0][3], r'^fid = {}, name = 0x[0-9a-f]+ '
                         r'"tt-sleeper"$'.format(o['sleeper']))

    def test_entry_param(self):
        self.assertEqual(self.out(BOX, 'entry.show').strip(),
                         'Printing of actual rlist entries is on')
        # Off: the items, not the entries.
        n = self.value(BOX, 'alive.len')
        out = value(self.out(BOX, 'alive.raw'))
        self.assertTrue(out.startswith(
            'rlist<fiber::link> of length {}, ref='.format(n)), out)
        items = re.findall(r'\[(\d+)\] = (0x[0-9a-f]+)', out)
        self.assertEqual([int(i) for i, _ in items], list(range(n)))
        self.assertEqual(int(items[0][1], 16),
                         self.value(BOX, 'alive.next'))

    def test_walk(self):
        n = self.value(BOX, 'alive.len')
        fwd = fids(self.out(BOX, 'alive'))
        # The first step shows the list, each next one entry, then the
        # walk is over and the next one starts again.
        self.assertIn('rlist<fiber::link> of length {},'.format(n),
                      self.out(BOX, 'walk.0'))
        got = []
        for i in range(1, n + 1):
            got += fids(self.out(BOX, 'walk.{}'.format(i)))
        self.assertEqual(got, fwd)
        for mode, err in self.errs(BOX, 'walk.{}'.format(n + 1)):
            self.assertEqual(err, 'No items left.', mode)
        self.assertIn('rlist<fiber::link> of length {},'.format(n),
                      self.out(BOX, 'walk.{}'.format(n + 2)))

    def test_unknown(self):
        # No entry type for a list the table cannot tell: a hint and the
        # items.  A list printed after an error is printed as ever.
        out = self.out(BOX, 'list.unknown')
        self.assertIn('failed to identify the type of the list entries', out)
        self.assertRegex(out, r'rlist<\?> of length \d+,')
        self.assertIn('no_such_type', self.out(BOX, 'list.bad_entry'))
        self.assertTrue(value(self.out(BOX, 'list.after')).startswith(
            'rlist<fiber::link> of length {},'.format(
                self.value(BOX, 'alive.len'))))

    def test_quorum(self):
        # The quorum triggers (gain and loss up to 3.5, change since 3.6):
        # empty here, but the entries are known.
        n = self.value(BOX, 'quorum.len')
        self.assertIn('rlist<trigger::link> of length {},'.format(n),
                      self.out(BOX, 'quorum'))

    def test_lut(self):
        # The entries of each release are in the table where its DWARF
        # has them.
        lut = set(self.out(BOX, 'lut').split())
        v = H.VERSION_TUPLE
        want = {
            'replicaset_on_quorum_gain': v < (3, 6),
            'replicaset_on_quorum_loss': v < (3, 6),
            'replicaset_on_quorum_change': v >= (3, 6),
            'txns': v >= (3, 4),
            'txn::in_all_txs': v < (3, 4),
            'vy_tx::in_writers': v < (3, 4),
            'memtx_tuple::in_gc': v < (3, 6),
            'memtx_block::in_gc': v >= (3, 6),
            'fiber::link': True,
        }
        self.assertEqual(dict((k, k in lut) for k in want), want)

    def test_txns(self):
        o = BOX.oracle()
        out = self.out(BOX, 'txns')
        self.assertIn('rlist<txn::in_{}> of length 1,'.format(
            'txns' if TXNS == 'txns' else 'all_txs'), out)
        self.assertEqual([f for _, _, _, f in entries(out)],
                         ['id = {}'.format(o['txn_id'])])

    def test_stailq(self):
        out = value(self.out(BOX, 'stmts'))
        self.assertEqual(len(entries(out)), 1, out)
        self.assertTrue(out.startswith(
            'stailq<txn_stmt::next> of length {},'.format(
                self.value(BOX, 'stmts.len'))), out)
        n = self.value(BOX, 'txn_cache.len')
        out = value(self.out(BOX, 'txn_cache'))
        self.assertTrue(out.startswith(
            'stailq<txn::in_txn_cache> of length {}'.format(n)), out)
        self.assertEqual(len(entries(out)), n)


class TestStructs(DbgCase):
    '''The printers of decimal_t, struct tt_uuid and struct datetime, on
    the cdata the Lua side made.'''
    SCENARIOS = (BOX,)

    def test_decimal(self):
        self.assertEqual(value(self.out(BOX, 'dec')), BOX.oracle()['dec'])

    def test_uuid(self):
        self.assertEqual(value(self.out(BOX, 'uuid')), BOX.oracle()['uuid'])

    def test_datetime(self):
        self.assertEqual(value(self.out(BOX, 'dt')), BOX.oracle()['dt'])


class TestMsgPack(DbgCase):
    SCENARIOS = (BOX,)

    def mp(self, name):
        return value(self.out(BOX, 'mp.' + name))

    def test_scalars(self):
        self.assertEqual(self.mp('scalars'), mp_text(
            [1, -2, 3.5, 'str', True, False, None, 300, -300, 2 ** 40]))
        self.assertEqual(self.mp('nested'), mp_text({'k': [1, {'x': 'y'}]}))
        # An empty array or map has no children: gdb prints nothing.
        self.assertEqual(self.mp('empty'), '{, }')
        self.assertEqual(self.mp('bin'), 'bin:YWJj')

    def test_decimal(self):
        self.assertEqual(self.mp('decimal'), 'dec:' + BOX.oracle()['dec'])
        self.assertEqual(self.mp('decimal_int'), 'dec:123456789012345')
        # All the digits of the build (76 since 3.5).
        self.assertEqual(self.mp('decimal_long'),
                         'dec:' + BOX.oracle()['dec_long'])

    def test_uuid(self):
        self.assertEqual(self.mp('uuid'), BOX.oracle()['uuid'])

    def test_datetime(self):
        self.assertEqual(self.mp('datetime'), BOX.oracle()['dt'])
        self.assertEqual(self.mp('datetime_tz'), BOX.oracle()['dt_tz'])

    def test_interval(self):
        self.assertEqual(self.mp('interval'),
                         '+1 years -2 months 3 days (none)')

    def test_error(self):
        e = BOX.oracle()['err']
        out = self.mp('error')
        self.assertTrue(out.startswith('{["stack"] = {{'), out)
        for k, v in (('type', e['type']), ('message', e['message']),
                     ('code', e['code']), ('line', e['trace'][0]['line'])):
            self.assertIn('["{}"] = {}'.format(k, mp_text(v)), out)

    def test_unknown_ext(self):
        self.assertEqual(self.mp('unknown_ext'), '{ext:99 of len 1, 1U}')

    def test_tuple_ext(self):
        # The keys are names, not MsgPack (as test-tarantool-gdb.py has).
        self.assertEqual(self.mp('tuple'),
                         '{[format_id] = 5U, [payload] = {1U, "x"}}')


if __name__ == '__main__':
    unittest.main()
