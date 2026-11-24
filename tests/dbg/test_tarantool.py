# Tests of tarantool_gdb.py against tarantool: every command and
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

import functools
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import harness as H
from harness import DbgCase, Scenario, gcval
from othercore import OtherCore
from test_cores import core_glob, kernel_core, raise_core_limit

ORACLE_TT = os.path.join(H.HERE, 'oracle_tt.py')
SOURCES = [H.TT_GDB, ORACLE_TT]
# The thread pointer as gdb names the register (TPIDR_EL0 is $tpidr).
# gdb 12 has none on AArch64 (the tpidr register came in gdb 13): there
# the tests take the one of tarantool_gdb.py, from the thread handle,
# which TestNoThreadPointer checks against the TLS ABI.
TP_REGISTER = '$fs_base' if H.ARCH != 'arm64' else \
    '$tpidr' if H.GDB_VERSION >= (13, 0) else None
TLS_BASE = TP_REGISTER or '$tl_var_addr(0)'


@functools.lru_cache(maxsize=None)
def tls_abi_offset(name):
    '''The offset from the thread pointer of the thread-local variable
    name of the tarantool executable, where the TLS ABI puts the TLS
    block of the executable, the first one: below the thread pointer on
    x86-64 (variant II: st_value - roundup(p_memsz, p_align) of PT_TLS),
    above it on AArch64, after the 16-byte TCB (variant I: roundup(16,
    p_align) + st_value).  The linker computes the same for the local-exec
    accesses of the executable.'''
    def readelf(*args):
        return subprocess.run(['readelf', '-W'] + list(args) + [H.TARANTOOL],
                              stdout=subprocess.PIPE, check=True,
                              universal_newlines=True).stdout
    m = re.search(r'^\s*TLS\s+0x[0-9a-f]+\s+(0x[0-9a-f]+)\s+0x[0-9a-f]+\s+'
                  r'0x[0-9a-f]+\s+(0x[0-9a-f]+)\s.*\s(0x[0-9a-f]+)$',
                  readelf('-l'), re.M)
    vaddr, memsz, align = (int(x, 16) for x in m.groups())
    assert vaddr % align == 0, 'PT_TLS at {:#x}, align {:#x}'.format(
        vaddr, align)
    m = re.search(r'^\s*\d+:\s+([0-9a-f]+)\s+\d+\s+TLS\s+\S+\s+\S+\s+\S+\s+'
                  r'{}$'.format(re.escape(name)), readelf('-s'), re.M)
    st_value = int(m.group(1), 16)

    def up(n):
        return (n + align - 1) // align * align
    return up(16) + st_value if H.ARCH == 'arm64' else st_value - up(memsz)


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
    # A UUID of the reserved variant (111x): any 128 bits since 3.2.
    ('uuid_variant', "'\\xd8\\x02' .. string.rep('\\x11', 8) .. '\\xe0' .. "
                     "string.rep('\\x22', 7)"),
    # An extension type tarantool does not have, in an array.
    ('unknown_ext', "'\\x92\\xd4\\x63\\x2a\\x01'"),
    # MP_COMPRESSION (5) of type 0 (none), raw size 3, 3 bytes of data.
    ('compression', "'\\xc7\\x05\\x05\\x00\\x03abc'"),
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
    # What lj-bt uses: fiber(), Cord().fibers(), FiberUnwinder's
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
                        ('tuple.format_id', 'eval:$t->format_id'),
                        ('threads.fibers', 'dbg-threads info tt-fibers'),
                        ('threads.tp', 'dbg-threads output *(long *){}'
                         .format(TLS_BASE)),
                    ])

# The same with gdb unable to read the thread pointer either, as gdb 12
# on AArch64: tarantool_gdb.py takes it from the thread handle.  The
# names are hidden from what it evaluates, not from the commands of the
# tests (`output cord_ptr`, `(long)&cord_ptr`).
NOTP = BOX.variant('box-notp', sources=[H.HIDE_ENUMS] + SOURCES,
                   gdb_env={'DBG_HIDE_ENUMS': NOTLS_HIDDEN +
                            ',$fs_base,$tpidr,$tpidr_el0'},
                   cmds=[
                       ('tl.offset', 'print $tl_var_find_offset(&main_cord)'),
                       ('tl.addr', 'print $tl_var_addr('
                        '$tl_var_find_offset(&main_cord))'),
                       ('tl.addr.gdb', 'eval:(long)&cord_ptr'),
                       ('tl.threads',
                        'dbg-threads output $tl_var_like(&main_cord)'),
                       ('tl.threads.gdb', 'dbg-threads output cord_ptr'),
                       ('tp', 'dbg-threads output (long)$tl_var_addr(0)'),
                       ('tp.cord_ptr', 'dbg-threads output (long)&cord_ptr'),
                       ('threads.fibers', 'dbg-threads info tt-fibers'),
                       ('threads.tp', 'dbg-threads output *(long *){}'
                        .format(TLS_BASE)),
                   ] + ([('tp.reg', 'dbg-threads output (long){}'.format(
                       TP_REGISTER))] if TP_REGISTER else []))

# Without a value or a type some printers need (a clang build may leave
# out an enum used only through its constants).
NODWARF = BOX.variant('box-nodwarf', sources=[H.HIDE_ENUMS] + SOURCES,
                      gdb_env={'DBG_HIDE_ENUMS': 'zones_unsorted,MP_ERROR_MAX,'
                               'FIELD_YEAR,JSON_TOKEN_NUM',
                               'DBG_HIDE_TYPES': 'tuple_flag'}, cmds=[
                          (k, c) for k, c in BOX_CMDS if k in (
                              'fibers', 'mp.datetime', 'mp.error',
                              'mp.interval', 'mp.decimal', 'mp.uuid', 'token',
                              'tuple', 'alive')])


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
    '''[(mode, {rank: output})] of a dbg-threads command; the output of
    every thread in the core must be the live one.  The core must hold
    the TLS of every thread, at the top of its stack: gcore of gdb before
    18.1 loses the thread stacks of glibc 2.42+, the harness writes the
    core with gcore-pages (see test_cores.py).'''
    live = per_thread(case.raw(scen, key, 'live')['out'])
    got = [('live', live)]
    if 'core' in scen.modes():
        core = per_thread(case.raw(scen, key, 'core')['out'])
        tp = dict((m, per_thread(case.raw(scen, 'threads.tp', m)['out']))
                  for m in ('live', 'core'))
        case.assertEqual(tp['core'], tp['live'], 'the TLS of the threads '
                         '(the TCB word) in the core and live')
        case.assertEqual(sorted(core), sorted(live), key)
        for r, s in core.items():
            case.assertEqual(s, live[r], '{} thread {}: the core output '
                             'differs'.format(key, r))
        got.append(('core', core))
    return got


# tt-fiber at a crash (#36): an FFI NULL write, and tarantool's crash
# handler calls abort().  Where gdb has the debug info of glibc 2.34+,
# the top frames are inline ones on x86_64: __pthread_kill_implementation
# and __pthread_kill_internal above __pthread_kill.  Live (gdb stops at
# the SIGSEGV, then at the SIGABRT of abort()), on the gcore core of that
# stop, and on the core the kernel writes when the SIGABRT kills the
# process (KernelCore).  fio starts the coio threads: a thread to switch
# to and back.
#
# Two fibers wait.  The oracle of their stacks is gdb's own bt of each
# one when it yielded, the fiber running then: a break at fiber_yield
# while it is the running fiber (FIBERS_SETUP), with no saved context
# and no tarantool_gdb.py in the unwinding.
FIBERS_LUA = '''
local fiber = require('fiber')

require('fio').stat('.')
fiber.self():name('tt-main')
local sleeper = fiber.new(function() fiber.sleep(1000) end)
sleeper:name('tt-sleeper')
local cond = fiber.cond()
local waiter = fiber.new(function() cond:wait() end)
waiter:name('tt-waiter')
fiber.yield()
'''

CRASH_LUA = FIBERS_LUA + '''
require('ffi').cast('int *', 0)[0] = 1 -- the crash
'''

FIBERS_BREAK = 'break fiber_yield if {}'.format(' || '.join(
    '$_streq(main_cord.fiber->name, "{}")'.format(n)
    for n in ('tt-sleeper', 'tt-waiter')))
# The two stops at FIBERS_BREAK, the first one is that of `run`.
FIBERS_SETUP = [
    'bt', 'eval:main_cord.fiber->fid', 'continue',
    'bt', 'eval:main_cord.fiber->fid', 'delete',
]


def fiber_bts(setup):
    '''{fid: [(level, pc or None, function)]}: gdb's own bt of each fiber
    at its fiber_yield, from the results of FIBERS_SETUP.'''
    return dict((setup[i + 1][1], bt_frames(setup[i][1])) for i in (0, 3))


def tt_fiber(name):
    return 'eval "tt-fiber %d", $dbg_fiber_id("{}")'.format(name)


CRASH_CMDS = [
    ('signal', 'eval:$_siginfo.si_signo'),
    ('libs', 'info sharedlibrary'),
    ('top', 'dbg-frame-types 8'),
    # Before the first bt: the frame filter of tarantool_gdb.py, once
    # there, prints some frames otherwise.
    ('fibers', 'info tt-fibers'),
    ('top.bt', 'bt'),
    ('sleeper', tt_fiber('tt-sleeper')),
    ('sleeper.frame', 'frame'),
    ('sleeper.bt', 'bt'),
    ('waiter', tt_fiber('tt-waiter')),
    ('waiter.frame', 'frame'),
    ('waiter.bt', 'bt'),
    # The selection survives a flush of the frame cache, and a thread
    # switch and back.
    ('flush', 'maint flush register-cache'),
    ('flush.bt', 'bt'),
    ('thread.2', 'thread 2'),
    ('thread.1', 'thread 1'),
    ('thread.1.bt', 'bt'),
    ('back', 'eval "tt-fiber %d", main_cord.fiber->fid'),
    ('back.bt', 'bt'),
    # What lj-bt does: set_fiber(), reset_fiber().
    ('ljbt.set', "python FiberUnwinder.instance().set_fiber([f for f in "
     "Cord().fibers() if f['name'].string() == 'tt-sleeper'][0])"),
    ('ljbt.set.bt', 'bt'),
    ('ljbt.reset', 'python FiberUnwinder.instance().reset_fiber()'),
    ('ljbt.reset.bt', 'bt'),
]

# gdb stops at the fibers' yields, then at the SIGSEGV; the SIGABRT of
# abort() is the stop.  A fiber selected at the SIGSEGV stays selected
# at the SIGABRT (live only); the commands start from the running fiber.
CRASH_SETUP = FIBERS_SETUP + [
    'continue',
    tt_fiber('tt-sleeper'),
    'continue',
    'bt',
    'eval "tt-fiber %d", main_cord.fiber->fid',
]
CRASH = Scenario('crash', CRASH_LUA, [FIBERS_BREAK], CRASH_CMDS,
                 setup=CRASH_SETUP, sources=SOURCES)


class KernelCore(object):
    '''The live run of a scenario, to its stop, then kill there lets the
    kernel write the core of the process (a signal whose default action
    dumps one).  run(): {'core': the result of the commands of the
    scenario on that core, 'live': the result of the live run} or
    {'reason': why there is no core}.'''

    PID = "python gdb.write('%d\\n' % gdb.selected_inferior().pid)"

    def __init__(self, scen, kill):
        self.scen = scen
        self.kill = kill
        self._res = None

    def run(self):
        if self._res is not None:
            return self._res
        wd = tempfile.mkdtemp(prefix='dbg-{}-kernel-'.format(self.scen.name))
        res = self._res = {'wd': wd, 'core': None}
        pattern, res['reason'] = kernel_core()
        if pattern is None:
            return res
        script = os.path.join(wd, 'script.lua')
        with open(script, 'w') as fh:
            fh.write(self.scen.text())
        plan = {'binary': H.TARANTOOL, 'args': [script], 'cwd': wd,
                'env': self.scen.env, 'sources': self.scen.sources,
                'deferred': self.scen.deferred, 'breaks': self.scen.breaks,
                'setup': self.scen.setup, 'prep': [], 'gcore': None,
                # gdb may fail the kill ("Couldn't get registers: No such
                # process.", a thread gone with the process): the core
                # tells what happened.
                'cmds': [['pid', self.PID], ['kill', self.kill]]}
        live = res['live'] = self.scen._gdb(wd, plan, 'kernel-live',
                                            preexec=raise_core_limit)
        pid = (live['cmds'].get('pid') or {}).get('out') or ''
        if live.get('fatal') or not pid.strip().isdigit():
            res['reason'] = 'the live run failed: {}'.format(
                live.get('fatal') or live['cmds'])
            return res
        found = glob.glob(core_glob(pattern, int(pid), wd))
        if not found:
            res['reason'] = 'no core {} was written ({}: {})'.format(
                core_glob(pattern, int(pid), wd), self.kill,
                live['cmds']['kill'])
            return res
        plan = {'binary': H.TARANTOOL, 'core': found[0],
                'sources': self.scen.sources, 'prep': self.scen.prep,
                'cmds': self.scen.cmds}
        res['core'] = self.scen._gdb(wd, plan, 'kernel')
        if H.KEEP != 'cores':
            os.unlink(found[0])
        return res

    def cleanup(self):
        if self._res is not None and not H.KEEP:
            shutil.rmtree(self._res['wd'], ignore_errors=True)


# At the SIGABRT stop `continue`: the signal kills the process.
CRASH_KERNEL = KernelCore(CRASH, 'continue')

# Where glibc's abort() stops under inline frames, with its debug info:
# x86_64.  On AArch64 (glibc 2.35, 2.39, 2.43 of Ubuntu 22.04, 24.04 and
# 26.04, dbg.yml run 45297778863) __pthread_kill_implementation is a
# normal frame 0 and __pthread_kill_internal a tail call (TAILCALL_FRAME)
# at 1.  gdb tries its inline and tail call unwinders before the Python
# ones, but the tail call one only for the caller of a frame its DWARF
# unwinder made (standard_unwinders, frame-unwind.c), which comes after
# them: such a frame was asked about first.  Nothing sits above the frame
# the fiber unwinder is asked about first, as on x86_64 without the debug
# info.
GLIBC_INLINE_TOP = H.ARCH == 'x64'

# A frame of bt.  gdb marks a return address it took a pointer
# authentication code off with " [PAC]" (AArch64, glibc of Ubuntu 26.04).
RX_BT = re.compile(r'^#(\d+) +(?:(0x[0-9a-f]+)(?: \[PAC\])? in )?'
                   r'([\w:<>~.]+)', re.M)


def bt_frames(out):
    '''[(level, pc or None, function)] of a bt.'''
    return [(int(n), int(pc, 16) if pc else None, f)
            for n, pc, f in RX_BT.findall(out)]


class TestBtFrames(unittest.TestCase):
    '''bt_frames() on what gdb prints, without gdb: the crash of CRASH
    on ubuntu-26.04-arm (gdb 17.1, cores.yml run 45297778873).'''

    def test_pac(self):
        out = (
            '#0  __pthread_kill_implementation (threadid=281474841976864, '
            'signo=signo@entry=6, no_tid=no_tid@entry=0) at '
            './nptl/pthread_kill.c:44\n'
            '#1  0x0000fffff7cde174 [PAC] in __pthread_kill_internal '
            '(threadid=<optimized out>, signo=6) at ./nptl/pthread_kill.c:89\n'
            '#2  0x0000fffff7c885fc in __GI_raise (sig=sig@entry=6) at '
            '../sysdeps/posix/raise.c:26\n'
            '#4  0x00000000005fed40 [PAC] in crash_signal_cb (signo=11, '
            'siginfo=<optimized out>, context=<optimized out>) at '
            '/tarantool/src/lib/core/crash.c:204\n'
            '#5  0x0000fffff7ffaa20 in <signal handler called> ()\n'
            '#6  __memcpy_sve () at ../sysdeps/aarch64/multiarch/'
            'memcpy_sve.S:71\n')
        self.assertEqual(bt_frames(out), [
            (0, None, '__pthread_kill_implementation'),
            (1, 0xfffff7cde174, '__pthread_kill_internal'),
            (2, 0xfffff7c885fc, '__GI_raise'),
            (4, 0x5fed40, 'crash_signal_cb'),
            (5, 0xfffff7ffaa20, '<signal'),
            (6, None, '__memcpy_sve')])


class CrashChecks(object):
    '''tt-fiber at the crash of CRASH, in MODE: live, core (the gcore core
    of the live stop) or kernel (the kernel's core of another live run,
    killed by the SIGABRT).'''
    MODE = None

    def crash(self):
        '''(the result of the commands, the live run) of MODE.'''
        if self.MODE == 'kernel':
            r = CRASH_KERNEL.run()
            if r['core'] is None:
                H.precondition(self, 'kernel-core', r['reason'])
            res, live = r['core'], r['live']
        else:
            res, live = CRASH.run()[self.MODE], CRASH.run()['live']
        self.assertIsNone(res.get('fatal'), res.get('fatal'))
        self.assertIsNone(live.get('fatal'), live.get('fatal'))
        return res, live

    def cmd(self, key):
        r = self.crash()[0]['cmds'][key]
        self.assertIsNone(r['err'], '{} ({}): {}'.format(key, self.MODE,
                                                         r['err']))
        if self.MODE == 'live' or not isinstance(r['out'], str):
            return r['out']
        return H.strip_core_warnings(r['out'])

    def test_crashed(self):
        # The stop, or the signal the core was written for: the SIGABRT
        # of abort().
        self.assertEqual(self.cmd('signal'), 6)

    def test_inline_top(self):
        # The precondition of the scenario: inline frames above the
        # innermost frame the fiber unwinder is asked about.
        top = [s.split(' ', 2) for s in self.cmd('top').strip().split('\n')]
        inline = 0
        while inline < len(top) and top[inline][1] == 'INLINE_FRAME':
            inline += 1
        if inline == 0 and not GLIBC_INLINE_TOP:
            # No frame above the one the unwinder is asked about first:
            # the bug of #36 cannot happen at this crash.
            self.assertEqual(top[0][1], 'NORMAL_FRAME', top)
            self.skipTest('glibc-inline-top: none on {} (glibc does not '
                          'inline __pthread_kill_implementation there, frame '
                          '0 is a normal one: {})'.format(H.ARCH, top[:2]))
        if inline == 0:
            libc = [s for s in self.cmd('libs').split('\n')
                    if re.search(r'/libc\.so\.6$', s.strip())]
            if not libc or '(*)' in libc[0]:
                why = 'gdb has no debug info for libc.so.6 (libc6-dbg)'
            else:
                why = 'gdb has the debug info of libc.so.6'
            H.precondition(self, 'inline-top', '{}: no inline frame at the '
                           'top of the crash ({})'.format(why, top))
        self.assertEqual(top[inline][1], 'NORMAL_FRAME', top)

    def test_switch(self):
        # bt after tt-fiber N is the stack of fiber N: the one gdb showed
        # at the entry of its fiber_yield, when it ran, under that frame.
        # The yield point itself, fiber_yield returning from the context
        # switch, is a frame of its own where the switch returns into it,
        # none where fiber_yield ends in a tail call (a PIE built from
        # source): the return address is its caller's then.
        live = self.crash()[1]
        oracle = fiber_bts(live['setup'])
        for name, inner in (('tt-sleeper', 'lbox_fiber_sleep'),
                            ('tt-waiter', 'fiber_cond_wait_timeout')):
            key = name[len('tt-'):]
            m = re.match(r'Switching stack unwinder to fiber (\d+) ',
                         self.cmd(key))
            self.assertIsNotNone(m, self.cmd(key))
            want = oracle.get(int(m.group(1)))
            self.assertIsNotNone(want, (name, sorted(oracle)))
            names = [f for _, _, f in want]
            self.assertEqual(names[0], 'fiber_yield', want)
            for f in (inner, 'lua_fiber_run_f', 'fiber_loop'):
                self.assertIn(f, names, (name, want))
            out = self.cmd(key + '.bt')
            got = [(pc, f) for _, pc, f in bt_frames(out)]
            if got and got[0][1] == 'fiber_yield':
                got = got[1:]
            self.assertEqual(got, [(pc, f) for _, pc, f in want[1:]],
                             '{} ({}): bt is not the one of the fiber at its '
                             'yield:\n{}'.format(name, self.MODE, out))

    def test_selected(self):
        # tt-fiber selects the newest frame of the fiber, the first one
        # of its bt, not an inline frame of the running fiber above it.
        for key in ('sleeper', 'waiter'):
            out = self.cmd(key + '.frame')
            self.assertEqual(bt_frames(out)[:1],
                             bt_frames(self.cmd(key + '.bt'))[:1],
                             '{} ({}): {}'.format(key, self.MODE, out))

    def test_flush(self):
        want = self.cmd('waiter.bt')
        self.assertNotEqual(want, self.cmd('top.bt'), 'no switch')
        self.assertEqual(self.cmd('flush.bt'), want, 'after a flush')
        self.assertEqual(self.cmd('thread.1.bt'), want,
                         'after a thread switch and back')

    def test_back(self):
        top = self.cmd('top.bt')
        self.assertEqual(self.cmd('back.bt'), top)
        self.assertEqual(self.cmd('ljbt.set.bt'), self.cmd('sleeper.bt'))
        self.assertEqual(self.cmd('ljbt.reset.bt'), top)


class TestCrashLive(CrashChecks, DbgCase):
    MODE = 'live'
    SCENARIOS = (CRASH,)

    def test_kept(self):
        # tt-fiber at the SIGSEGV (lj_cconv_ct_ct at frame 0), `continue`
        # to the SIGABRT: bt there is still the fiber's, from under the
        # inline frames of the new stop.
        setup = self.result(CRASH, 'live')['setup']
        self.assertEqual([err for _, _, err in setup],
                         [None] * len(CRASH_SETUP), setup)
        self.assertEqual(setup[len(FIBERS_SETUP) + 3][1],
                         self.cmd('sleeper.bt'))


# The selection when the thread runs: tt-fiber at a breakpoint, then
# `stepi` and `continue` to the next stop in the same fiber.  gdb steps
# over the breakpoint at the pc first, unwinding the frames at the old
# registers after the thread was resumed; at the stop the innermost
# frame has new ones.
STEP_LUA = '''
local fiber = require('fiber')

local sleeper = fiber.new(function() fiber.sleep(1000) end)
sleeper:name('tt-sleeper')
fiber.yield()
print('one') -- the stop
print('two') -- the next one
os.exit(0)
'''

STEP = Scenario('fiber-step', STEP_LUA, ['break lj_cf_print'], [
    # Before tt-fiber, which selects a frame of the fiber.
    ('sleeper.pc', 'eval:$pc'),
    ('sleeper', tt_fiber('tt-sleeper')),
    ('sleeper.bt', 'bt'),
    ('stepi', 'stepi'),
    ('stepi.pc', 'eval:$pc'),
    ('stepi.bt', 'bt'),
    ('cont', 'continue'),
    ('cont.arg', 'eval:$_streq((char *)(' + strdata(0) + '), "two")'),
    ('cont.bt', 'bt'),
], sources=SOURCES, live_only='the thread runs')


class TestFiberStep(DbgCase):
    SCENARIOS = (STEP,)

    def test_stepi(self):
        self.assertNotEqual(self.out(STEP, 'stepi.pc'),
                            self.out(STEP, 'sleeper.pc'))
        want = self.out(STEP, 'sleeper.bt')
        self.assertIn('lbox_fiber_sleep', want)
        self.assertEqual(self.out(STEP, 'stepi.bt'), want)

    def test_continue(self):
        self.assertEqual(self.out(STEP, 'cont.arg'), 1)
        self.assertEqual(self.out(STEP, 'cont.bt'),
                         self.out(STEP, 'sleeper.bt'))


class TestCrashCore(CrashChecks, DbgCase):
    MODE = 'core'
    SCENARIOS = (CRASH,)


class TestCrashKernelCore(CrashChecks, DbgCase):
    MODE = 'kernel'
    SCENARIOS = (CRASH_KERNEL,)


class TestThreadLocal(DbgCase):
    SCENARIOS = (BOX,)

    def live(self, key):
        return self.raw(BOX, key, 'live')['out']

    def test_find_offset(self):
        # The offset of cord_ptr, the variable that holds &main_cord in
        # the main thread; nothing else printed.  Where the TLS ABI puts
        # it, and where gdb's own lookup finds it from the thread pointer.
        self.assertEqual(value(self.out(BOX, 'tl.offset')),
                         str(tls_abi_offset('cord_ptr')))
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
        # Another process: the format id may differ.
        out = value(self.out(NOTLS, 'tuple'))
        self.assertIn('format_id = {},'.format(
            self.value(NOTLS, 'tuple.format_id')), out)
        self.assertTrue(out.endswith('data = {}}}'.format(
            mp_text(NOTLS.oracle()['tuple']))), out[-300:])


class TestNoThreadPointer(DbgCase):
    '''gdb cannot read the thread pointer (gdb 12 on AArch64, cores.yml
    run 45154577020): tarantool_gdb.py takes it from the thread handle of
    libthread_db, and the thread-local functions work as with it.'''
    SCENARIOS = (BOX, NOTP)

    def test_thread_pointer(self):
        # In every thread cord_ptr is where the TLS ABI puts it from that
        # thread pointer (gdb's own lookup, live), and the thread pointer
        # is the register where gdb has one, live and on the core.
        off = tls_abi_offset('cord_ptr')
        tp = per_thread(self.raw(NOTP, 'tp', 'live')['out'])
        cord = per_thread(self.raw(NOTP, 'tp.cord_ptr', 'live')['out'])
        self.assertGreater(len(tp), 2)
        self.assertEqual(sorted(tp), sorted(cord))
        for rank, s in tp.items():
            self.assertNotIn('!error', s, rank)
            self.assertEqual(int(s) + off, int(cord[rank]), rank)
        got = thread_outs(self, NOTP, 'tp')
        if TP_REGISTER:
            for mode, outs in got:
                self.assertEqual(outs, per_thread(
                    self.raw(NOTP, 'tp.reg', mode)['out']), mode)

    def test_functions(self):
        self.assertEqual(value(self.out(NOTP, 'tl.offset')),
                         str(tls_abi_offset('cord_ptr')))
        self.assertEqual(value(self.out(NOTP, 'tl.addr')),
                         '(void *) 0x{:x}'.format(
                             self.value(NOTP, 'tl.addr.gdb')))
        want = per_thread(self.raw(NOTP, 'tl.threads.gdb', 'live')['out'])
        self.assertGreater(len(want), 2)
        for mode, got in thread_outs(self, NOTP, 'tl.threads'):
            for rank, s in got.items():
                self.assertEqual(re.sub(r'^\(.*?\) ', '', s),
                                 re.sub(r'^\(.*?\) ', '', want[rank]),
                                 (mode, rank))
        self.assertNoPyError(NOTP)

    def test_threads(self):
        want = dict((r, [(f, n) for _, f, n, _ in fiber_rows(s)])
                    for r, s in per_thread(self.raw(
                        BOX, 'threads.fibers', 'live')['out']).items())
        for mode, got in thread_outs(self, NOTP, 'threads.fibers'):
            for rank, text in got.items():
                self.assertNotIn('!error', text, (mode, rank))
                self.assertEqual([(f, n) for _, f, n, _ in fiber_rows(text)],
                                 want[rank], (mode, rank))


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


class TestNoDwarf(DbgCase):
    '''What a printer needs is not in the debug info: that printer is off,
    the rest of the file works.'''
    SCENARIOS = (BOX, NODWARF)

    def test_load(self):
        for mode in NODWARF.modes():
            res = self.result(NODWARF, mode)
            self.assertEqual(res['load_err'], [], mode)
            self.assertEqual(
                [s for s in res['load'].split('\n')
                 if s.startswith('tarantool_gdb.py:')],
                ['tarantool_gdb.py: no value {} in the debug info, the {} '
                 'printer is off'.format(n, p) for n, p in (
                     ('zones_unsorted', 'datetime'),
                     ('MP_ERROR_MAX', 'MsgPack error'),
                     ('FIELD_YEAR', 'MsgPack interval'),
                     ('JSON_TOKEN_NUM', 'json_token'))], mode)
        self.assertNoPyError(NODWARF)

    def test_off(self):
        # The extensions with no printer print as unknown ones.
        for name, ext in (('datetime', 4), ('error', 3), ('interval', 6)):
            self.assertRegex(value(self.out(NODWARF, 'mp.' + name)),
                             r'^ext:{} of len \d+$'.format(ext))
        self.assertTrue(value(self.out(NODWARF, 'token')).startswith(
            '{type = '))
        # No tuple_flag: the flags in hex only.
        out = value(self.out(NODWARF, 'tuple'))
        self.assertRegex(out, r'flags = 0x[0-9a-f]{2}, format_id')

    def test_rest(self):
        # Another process: the same objects at other addresses.
        for key in ('mp.decimal', 'mp.uuid'):
            self.assertEqual(value(self.out(NODWARF, key)),
                             value(self.out(BOX, key)), key)
        self.assertEqual(
            [(f, n) for _, f, n, _ in fiber_rows(self.out(NODWARF, 'fibers'))],
            [(f, n) for _, f, n, _ in fiber_rows(self.out(BOX, 'fibers'))])
        self.assertEqual(fids(self.out(NODWARF, 'alive')),
                         fids(self.out(BOX, 'alive')))


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
        self.assertEqual(self.mp('uuid_variant'),
                         '11111111-1111-1111-e022-222222222222')

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

    def test_compression(self):
        # The name of the type comes from compression_type_strs.
        self.assertEqual(self.mp('compression'),
                         'compression(none):[3]->[3]')

    def test_tuple_ext(self):
        # The keys are names, not MsgPack (as test-tarantool-gdb.py has).
        self.assertEqual(self.mp('tuple'),
                         '{[format_id] = 5U, [payload] = {1U, "x"}}')


class TestOtherCore(DbgCase):
    '''Every command of BOX on a core after a core of another process
    (see othercore.py): the printers read their arrays at the address of
    this process, a tt-list-walk of main_cord.alive starts over (on a
    static build the list head is at the same address in both).'''
    OTHER = OtherCore(BOX)
    SCENARIOS = OTHER.scenarios

    def test_box(self):
        self.assertEqual(self.OTHER.diffs(), [])


if __name__ == '__main__':
    unittest.main()
