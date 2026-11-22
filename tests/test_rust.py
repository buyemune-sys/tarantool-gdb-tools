# The `rust` group: lj-bt on stacks where Lua and Rust modules call each
# other.
#
# tests/lua/rust.lua runs the scenarios with the modules of tests/rust,
# which setUpModule builds with tests/rust/build.sh (cargo; without cargo
# the group is skipped):
#
# * plain, on the bare Lua C API, in four builds (debuginfo, release
#   without debuginfo, stripped, panic = "abort"): Lua -> Rust ->
#   lua_call -> Lua -> Rust -> Lua -> a box C function (basic), 30 levels
#   of Lua <-> Rust recursion (rec), a Lua error thrown through the Rust
#   frames (error), a Rust function called through FFI from a hot loop,
#   a JIT trace (ffi);
# * tnt, on the `tarantool` crate: a tlua function that calls Lua in a new
#   Lua thread (tlua), a stored procedure on Rust called through box.func
#   (proc) and through an iproto call (iproto), fibers created from Rust
#   (fibers).
#
# The Lua frames are checked against debug.getinfo (the oracle), the C
# frames against `bt -no-filters`, the VM bands and the Rust frames
# between them against the known structure of each chain.  The stops of
# the debuginfo build (JIT on) and of tnt are also written with gcore: on
# the core files lj-bt must give what it gave on the live process.
#
# Known errors of lj-bt are expectedFailure tests, with the reason.
#
# Environment: LJBT_RUST_TARGET, where build.sh puts the modules (default
# tests/rust/target); CARGO, the cargo binary.

import os
import platform
import re
import shutil
import subprocess
import unittest

import common as C

RUST = os.path.join(C.TESTS, 'rust')
TARGET = os.environ.get('LJBT_RUST_TARGET') or os.path.join(RUST, 'target')
SCENARIO = os.path.join(C.LUA, 'rust.lua')
PROBE_RUST = os.path.join(C.TESTS, 'probe_rust.py')
RECURSION = 30                  # rust.lua

with open(SCENARIO) as _fh:
    SRC = _fh.read().splitlines()


def line_of(text):
    return next(i + 1 for i, s in enumerate(SRC) if text in s)


DEF = {
    'target': line_of('local function target(x)'),
    'inner': line_of('function _G.ljbt_basic_inner('),
    'cb': line_of('function _G.ljbt_basic_cb('),
    'basic': line_of('function _G.ljbt_basic('),
    'rec': line_of('function _G.ljbt_rec('),
    'thrower': line_of('local function thrower()'),
    'err': line_of('function _G.ljbt_err('),
    'ffi': line_of('function _G.ljbt_ffi('),
    'from_tlua': line_of('function _G.ljbt_from_tlua('),
    'from_proc': line_of('function _G.ljbt_from_proc('),
    'tlua': line_of('function _G.ljbt_tlua('),
    'proc_func': line_of('function _G.ljbt_proc_func('),
    'fiber_body': line_of('function _G.ljbt_fiber_body('),
    'D': line_of('local function D()'),
    'main': 0,
}
FFI_CALL_LINE = line_of('r = lib.ljbt_ffi_hot(args[i])')
FIBER_SLEEP_LINE = line_of('FIBER_SLEEP_LINE')


def bottom(scenario):
    '''What a chain of rust.lua runs on: RUN.<scenario>() called by D() of
    the main chunk.'''
    return [line_of('function RUN.{}()'.format(scenario)), DEF['D'],
            DEF['main']]


# Rust frames by their name, whatever the build calls them
# (ljbt_plain::{impl#0}::entry from DWARF, <ljbt_plain::Call as
# ljbt_plain::LuaEntry>::entry from the symbol table).
ROLES = [('invoke', r'::invoke$'), ('l_call', r'::l_call$'),
         ('entry', r'::entry$'), ('hot', r'(^|::)ljbt_ffi_hot$'),
         ('lookup', r'::lookup$'), ('call_global', r'::call_global$'),
         ('proc', r'::ljbt_proc$'),
         ('wrapper', r'^tlua::functions_write::wrapper<'),
         ('lua_fiber', r'::lua_fiber$'), ('nap', r'::nap$'),
         ('rust_fiber', r'::rust_fiber$'),
         ('trampoline', r'::trampoline_for_ffi<')]

BUILD = {}                      # module -> None, or why it is not built


def lib(profile, module):
    return os.path.join(TARGET, profile, 'lib{}.so'.format(module))


def setUpModule():
    cargo = os.environ.get('CARGO') or shutil.which('cargo')
    if cargo is None:
        raise unittest.SkipTest(
            'cargo not found: the rust group builds its modules with it '
            '(tests/rust/build.sh; or set CARGO)')
    for m in ('plain', 'tnt'):
        try:
            p = subprocess.run(
                [os.path.join(RUST, 'build.sh'), m], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                universal_newlines=True, timeout=240,
                env=dict(os.environ, CARGO=cargo, LJBT_RUST_TARGET=TARGET))
            BUILD[m] = None if p.returncode == 0 else \
                'tests/rust/build.sh {} failed:\n{}'.format(
                    m, '\n'.join(p.stdout.splitlines()[-30:]))
        except subprocess.TimeoutExpired:
            BUILD[m] = 'tests/rust/build.sh {} timed out'.format(m)


def arg_register():
    '''The register of the first integer argument at a function entry.'''
    return '$x0' if platform.machine() in ('aarch64', 'arm64') else '$rdi'


def role(name):
    if name is None:
        return None
    for r, rx in ROLES:
        if re.search(rx, name):
            return r
    return name


def lua_tokens(entries):
    '''lj-bt entries (newest first) as tokens, oldest first: ('L',
    defline, line), ('B', None) for C functions and builtins, ('H',
    defline) for a tail call line (rust.lua has no tail calls).'''
    res = []
    for e in reversed(entries):
        if e['kind'] == 'heuristic':
            res.append(('H', e['defline']))
        elif e.get('fkind') == 'lua':
            res.append(('L', e['defline'], e['currentline']))
        elif e.get('fkind') in ('c', 'ff'):
            res.append(('B', None))
        else:
            res.append(('?', e.get('text')))
    return res


def oracle_tokens(levels):
    '''debug.getinfo levels (newest first) as tokens, oldest first, and
    the C function or builtin the caller is about to call.'''
    res = []
    for lv in reversed(levels):
        if lv['what'] in ('Lua', 'main'):
            res.append(('L', lv['linedefined'], lv['currentline']))
        else:
            res.append(('B', None))
    return res + [('B', None)]


def is_rust(rec, f, module):
    so = rec['solibs'].get(f['pc'])
    return so is not None and module in os.path.basename(so)


def shape(rec, module):
    '''The chain as lj-bt shows it, newest first: ('vm', [deflines of the
    rust.lua Lua frames]) for a VM band, ('R', role) for a Rust frame of
    the module (inlined frames left out).'''
    res = []
    for f in rec['ljbt']['frames']:
        if f['kind'] == 'vm':
            res.append(('vm', [e['defline'] for e in f['lua']
                               if e.get('fkind') == 'lua' and
                               (e.get('chunk') or '').endswith('rust.lua')]))
        elif is_rust(rec, f, module) and not f['inline']:
            res.append(('R', role(f['name'])))
    return res


def bands(doc, L=None):
    return [f for f in doc['frames'] if f['kind'] == 'vm' and
            (L is None or f['L'] == L)]


def unfolded_c(doc):
    '''Lua C functions shown as Lua entries, not folded into a C frame.'''
    return [e for f in doc['frames'] for e in f['lua']
            if e['kind'] == 'lua' and e.get('fkind') == 'c']


def core_shape(doc):
    '''What must not depend on live vs core (test_core.shape).'''
    res = []
    for f in doc['frames']:
        res.append((f['level'], f['kind'], f['name'],
                    (f['folded'] or {}).get('text'),
                    tuple(e['text'] for e in f['lua'])))
    return res, list(doc['notes'])


class RustRun(object):
    '''One tarantool under gdb with rust.lua; the probe records by tag.'''
    module = None
    profile = 'ljbt-debug'
    scenarios = ()
    jit = 'on'
    cores = False
    tt_gdb = False              # the run loads tarantool-gdb.py

    @classmethod
    def env(cls):
        return {}

    @classmethod
    def script(cls, probe, gcore):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        why = BUILD.get(cls.module[len('ljbt_'):], 'not built')
        if why:
            raise RuntimeError(why)
        cls.wd = C.WorkDir('rust')
        probe = cls.wd.file('probe.jsonl')
        gcore = '--gcore {}'.format(cls.wd.path) if cls.cores else ''
        env = dict(cls.env(), LJBT_RUST_SCENARIOS=','.join(cls.scenarios),
                   LJBT_JIT_MODE=cls.jit)
        saved = dict((k, os.environ.get(k)) for k in env)
        os.environ.update(env)
        try:
            cls.rc, cls.out = C.run_gdb(
                cls.wd, C.gdb_prelude() + 'source {}\n'.format(PROBE_RUST) +
                cls.script(probe, gcore), [SCENARIO])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cls.recs = C.read_jsonl(probe)
        cls.oracle = dict((r['seq'], r['levels'])
                          for r in C.read_jsonl(cls.wd.file('oracle.jsonl')))
        cls.failures = C.read_jsonl(cls.wd.file('failures.jsonl'))
        cls.results = C.read_jsonl(cls.wd.file('results.jsonl'))

    @classmethod
    def tearDownClass(cls):
        for r in cls.recs:
            if r.get('core') and os.path.exists(r['core']):
                os.unlink(r['core'])
        cls.wd.cleanup()

    def stop(self, tag, seq=None):
        recs = [r for r in self.recs if r['tag'] == tag and
                (seq is None or r['seq'] == seq)]
        self.assertEqual(len(recs), 1, 'stops {} seq {}: {}'.format(
            tag, seq, len(recs)))
        rec = recs[0]
        self.assertIsNone(rec['error'], rec['error'])
        return rec

    def done(self, name):
        return {'scenario': name, 'done': True} in self.results

    def assert_clean(self, rec):
        '''check_doc: the C frames of bt -no-filters, no "!!", every frame
        link once, as many frames as lj-stack.'''
        probs = C.check_doc(rec)
        self.assertEqual(probs, [], '\n'.join(probs))

    def assert_oracle(self, rec, seq):
        levels = self.oracle.get(seq)
        self.assertIsNotNone(levels, 'no oracle record')
        doc = rec['ljbt']
        mine = lua_tokens([e for e in C.entries(doc)
                           if e.get('L') == doc['L']])
        self.assertEqual(mine, oracle_tokens(levels))

    def assert_rust_names(self, rec):
        '''Rust frames carry demangled names (or none, stripped).'''
        for f in rec['ljbt']['frames']:
            if is_rust(rec, f, self.module) and f['name'] is not None:
                self.assertFalse(f['name'].startswith(('_R', '_ZN')),
                                 f['name'])
                self.assertIn('::', f['name'])

    def test_ran(self):
        self.assertNotIn('<timeout>', self.out)
        self.assertEqual(self.failures, [])
        self.assertEqual([r for r in self.recs if r['error']], [])

    # Stops whose cores a test of their own checks (a core is checked
    # once: check_core removes it).
    core_apart = ()

    def test_core(self):
        self.check_cores([t for t in self.tags() if t not in
                          self.core_apart])

    def check_cores(self, tags):
        '''check_core of the cores of these tags; {tag: the record of the
        core}.'''
        if not self.cores:
            self.skipTest('no core files in this run')
        live = [r for r in self.recs if r.get('core') and r['tag'] in tags]
        self.assertTrue(live, 'no core files written')
        dead = {}
        for rec in live:
            with self.subTest(tag=rec['tag'], seq=rec['seq']):
                dead[rec['tag']] = self.check_core(rec)
        return dead

    def tags(self):
        return sorted(set(r['tag'] for r in self.recs))

    def solib_dir(self):
        '''A directory for solib-search-path with the module under the
        names of its copies that are gone: box.func loads a C module from a
        copy in $TMPDIR/tntXXXXXX/<package>.so and deletes the copy right
        after dlopen (module_cache.c), so gdb finds no file, and no
        symbols, for it in a core.'''
        d = self.wd.file('solibs')
        if not os.path.isdir(d):
            os.mkdir(d)
            for so in set(v for r in self.recs
                          for v in r['solibs'].values() if v):
                name = os.path.basename(so)
                if not os.path.exists(so) and self.module[len('ljbt_'):] \
                        in name and not os.path.lexists(os.path.join(d,
                                                                     name)):
                    os.symlink(lib(self.profile, self.module),
                               os.path.join(d, name))
        return d

    def check_core(self, live):
        out = self.wd.file('core-probe.{}-{}.jsonl'.format(live['seq'],
                                                          live['tag']))
        # tarantool-gdb.py only if the live run had it (it adds an unwinder).
        tt = 'ljbt-probe-tt {}\n'.format(C.TT_GDB) if C.TT_GDB and \
            self.tt_gdb else ''
        opts = '--all-fibers' if 'all_fibers' in live else ''
        # The extensions evaluate C expressions: load them with the
        # language C, as before `run` on the live process (frame #0 of a
        # core may be a Rust frame); lj-bt runs with the language of the
        # frame, as it did live.
        script = 'set language c\n' + C.gdb_prelude() + \
            'source {}\n'.format(PROBE_RUST) + \
            'set solib-search-path {}\n'.format(self.solib_dir()) + tt + \
            'set language auto\n' + \
            'ljbt-rust-probe {} --tag {} {}\n'.format(out, live['tag'], opts)
        fibers = [r for r in self.recs if r['fiber'] is not None and
                  live['tag'] == 'target' and live['seq'] == 8]
        if fibers:
            script += 'ljbt-rust-fibers {} {}\n'.format(
                out, self.wd.file('results.jsonl'))
        rc, text = C.run_gdb(self.wd, script, [], core=live['core'])
        os.unlink(live['core'])         # Hundreds of MB, even with LJBT_KEEP.
        dead = dict((r['tag'], r) for r in C.read_jsonl(out))
        self.assertIn(live['tag'], dead, text)
        for r in [live] + fibers:
            d = dead.get(r['tag'])
            self.assertIsNotNone(d, '{} not probed on the core'.format(
                r['tag']))
            self.assertIsNone(d['error'], d['error'])
            self.assertEqual(core_shape(d['ljbt']), core_shape(r['ljbt']))
            self.assertIsNone(C.cframes_mismatch(d['ljbt'], d['bt']))
        if 'all_fibers' in live:
            self.assertIn('all_fibers', dead[live['tag']],
                          dead[live['tag']].get('all_fibers_error'))
            mine = [(x['fiber'], x['bt'] and core_shape(x['bt']))
                    for x in dead[live['tag']]['all_fibers']]
            theirs = [(x['fiber'], x['bt'] and core_shape(x['bt']))
                      for x in live['all_fibers']]
            self.assertEqual(mine, theirs)
        return dead[live['tag']]


# The plain module. ##############################################


class PlainMixin(RustRun):
    module = 'ljbt_plain'
    profile = 'ljbt-debug'
    scenarios = ('basic', 'rec', 'error', 'ffi')
    # Rust roles that keep a name in this build (strip: only the exported
    # FFI function).
    named = None

    @classmethod
    def env(cls):
        return {'LJBT_RUST_PLAIN': lib(cls.profile, 'ljbt_plain')}

    @classmethod
    def script(cls, probe, gcore):
        p = 'ljbt-rust-probe {} --tag {{}} {}'.format(probe, gcore)
        return '\n'.join([
            'set breakpoint pending on',
            # The targets of chain.lua.
            'break lbox_session_exists if $ljbt_arg(L, 0) >= 4000',
            'commands', 'silent', p.format('target'), 'continue', 'end',
            'break box_space_id_by_name if len >= 40',
            'commands', 'silent', p.format('target'), 'continue', 'end',
            # The error of the error scenario, not the others (the JIT
            # throws one whenever a trace recording aborts).
            'break lj_err_throw if $ljbt_err_is(L, "ljbt-boom")',
            'commands', 'silent', p.format('throw'), 'continue', 'end',
            # The entry of the FFI function, the last call of the loop.
            'break ljbt_ffi_hot if ({} & 0xffffffff) >= 4000'.format(
                arg_register()),
            'commands', 'silent', p.format('ffi-entry'), 'continue', 'end',
            'run',
            # The abort build stops here, at the SIGABRT.
            'python',
            'if gdb.selected_inferior().pid:',
            "    gdb.execute('ljbt-rust-probe {} --tag end')".format(probe),
            "    gdb.execute('kill')",
            'end', ''])

    def R(self, r):
        '''A Rust frame of the given role, as this build names it.'''
        return ('R', r if self.named is None or r in self.named else None)

    def test_scenarios_done(self):
        for s in self.scenarios:
            self.assertTrue(self.done(s), s)
        self.assertIn({'done': True}, self.results)
        self.assertRegex(self.out, r'exited normally')

    # basic ##############################################################

    def test_basic(self):
        rec = self.stop('target', 1)
        self.assert_clean(rec)
        self.assert_oracle(rec, 1)
        self.assert_rust_names(rec)
        # Lua above and below each Rust part, the Rust frames in between;
        # all in one lua_State.
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['target'], DEF['inner']]),
            self.R('invoke'), self.R('entry'),
            ('vm', [DEF['cb']]),
            self.R('invoke'), self.R('l_call'),
            ('vm', [DEF['basic']] + bottom('basic'))])
        doc = rec['ljbt']
        self.assertEqual(len(set(f['L'] for f in bands(doc))), 1)

    def test_rust_functions_folded(self):
        # The Lua C functions call and call_t are the Rust frames right
        # above their VM bands: lj-bt folds them there.
        doc = self.stop('target', 1)['ljbt']
        self.assertEqual([e['text'] for e in unfolded_c(doc)], [])
        folded = [role(f['folded']['func']) for f in doc['frames']
                  if f['folded'] and f['folded'].get('fkind') == 'c']
        self.assertEqual(folded, ['lbox_session_exists', 'entry', 'l_call'])

    # rec ################################################################

    def test_rec(self):
        rec = self.stop('target', 2)
        self.assert_clean(rec)
        self.assert_oracle(rec, 2)
        exp = [('vm', [DEF['target'], DEF['rec']])]
        for k in range(RECURSION):
            exp += [self.R('invoke'), self.R('l_call'),
                    ('vm', [DEF['rec']] + (bottom('rec') if k == RECURSION - 1
                                           else []))]
        self.assertEqual(shape(rec, self.module), exp)

    def test_rec_counts(self):
        doc = self.stop('target', 2)['ljbt']
        base = self.stop('target', 1)['ljbt']
        # One VM entry per lua_call: basic has 2, rec has RECURSION.
        self.assertEqual(len(bands(doc, doc['L'])) -
                         len(bands(base, base['L'])), RECURSION - 2)
        lua = [e for e in C.entries(doc) if e.get('fkind') == 'lua' and
               e['defline'] == DEF['rec']]
        self.assertEqual(len(lua), RECURSION + 1)
        links = [e['link'] for e in C.entries(doc) if e.get('link')]
        self.assertEqual(len(links), len(set(links)))

    # error ##############################################################

    def test_error_stop(self):
        rec = self.stop('throw')
        self.assert_clean(rec)
        self.assert_oracle(rec, 3)
        frames = rec['ljbt']['frames']
        self.assertEqual(frames[0]['name'], 'lj_err_throw')
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['thrower']]),
            self.R('invoke'), self.R('l_call'),
            ('vm', [DEF['err']] + bottom('error'))])

    def test_error_caught(self):
        # The error went through the Rust frames to the pcall below them,
        # the process lives on.
        res = [r for r in self.results if r.get('scenario') == 'error' and
               'ok' in r]
        self.assertEqual(len(res), 1, self.results)
        self.assertFalse(res[0]['ok'])
        self.assertIn('ljbt-boom', res[0]['err'])

    # ffi ################################################################

    def test_ffi_from_rust(self):
        # box_space_id_by_name called by Rust, called through FFI from the
        # hot loop.
        rec = self.stop('target', 4)
        self.assert_clean(rec)
        frames = rec['ljbt']['frames']
        self.assertEqual(frames[0]['name'], 'box_space_id_by_name')
        self.assertEqual(shape(rec, self.module), [
            self.R('lookup'), self.R('hot'),
            ('vm', [DEF['ffi']] + bottom('ffi'))])
        self.check_ffi_caller(rec, frames[3])

    def test_ffi_entry(self):
        # The stop is in the Rust function itself.
        rec = self.stop('ffi-entry')
        self.assert_clean(rec)
        frames = rec['ljbt']['frames']
        self.assertEqual(role(frames[0]['name']), 'hot')
        self.assertEqual(shape(rec, self.module), [
            self.R('hot'), ('vm', [DEF['ffi']] + bottom('ffi'))])
        self.check_ffi_caller(rec, frames[1])

    def check_ffi_caller(self, rec, caller):
        '''The caller of the FFI function: a JIT trace (JIT on) or the FFI
        call of the interpreter; then the band of the hot loop.'''
        doc = rec['ljbt']
        lua = [e for e in C.entries(doc) if e.get('L') == doc['L']]
        if self.jit == 'on':
            self.assertEqual(caller['kind'], 'trace', caller['text'])
            self.assertIn('bt_raw', rec)
            k = doc['frames'].index(caller)
            self.assertEqual(doc['frames'][k + 1]['kind'], 'vm')
            self.assertTrue(doc['frames'][k + 1]['synthetic'])
            # The hot loop is the trace's base frame (its line comes from
            # a snapshot).
            self.assertEqual(lua[0]['defline'], DEF['ffi'])
            self.assertIn('base frame, snapshot', lua[0]['note'] or '')
        else:
            self.assertEqual(caller['kind'], 'c')
            self.assertEqual([(e['fkind'], e['func']) for e in lua[:1]],
                             [('ff', 'ffi.meta.__call')])
            self.assertEqual((lua[1]['defline'], lua[1]['currentline']),
                             (DEF['ffi'], FFI_CALL_LINE))


class RustFrameStopMixin(object):
    '''Builds with DWARF: at a stop in a Rust function frame #0 has the
    language rust, and gdb parses expressions and looks types up in it.
    What lj-bt evaluates is C: the addresses of the VM symbols
    ('(unsigned long)&lj_vm_asm_begin', without them no vm_begin, vm_end
    and lj_BC_JLOOP: JIT on, the trace unwinder gave up and the frames
    behind the trace were garbage; JIT off, lj_vm_ffi_call was taken for
    VM code), lj_bc_mode, the types of the LuaJIT extension ("void" is
    no type in Rust).  lj-bt evaluates in C and gives the language
    back.'''

    def test_ffi_entry(self):
        PlainMixin.test_ffi_entry(self)
        rec = self.stop('ffi-entry')
        self.assertEqual(rec['language'], ['auto', 'rust'])
        self.assertEqual(rec['language_after'], rec['language'])
        self.assertEqual(rec['language_error'], rec['language'])

    core_apart = ('ffi-entry',)

    def test_core_rust_frame(self):
        # The same stop on its core, where the first lj-bt of the session
        # runs in the Rust frame, before any type cache is filled (live,
        # the stops before it filled them in C frames): mref('void *', ...)
        # of LJBTProto, lj_bc_mode of LJBTConf.bcmode_a.
        for d in self.check_cores(self.core_apart).values():
            self.assertEqual(d['language'], ['auto', 'rust'])
            self.assertEqual(d['language_after'], ['auto', 'rust'])


class TestRustPlainDebug(RustFrameStopMixin, PlainMixin, unittest.TestCase):
    cores = True

    def test_names(self):
        # gdb names the Rust frames from DWARF.
        doc = self.stop('target', 1)['ljbt']
        names = [f['name'] for f in doc['frames'] if '::' in (f['name']
                                                              or '')]
        self.assertEqual(names[:3], ['ljbt_plain::invoke',
                                     'ljbt_plain::{impl#0}::entry',
                                     'ljbt_plain::invoke'])


class TestRustPlainRelease(PlainMixin, unittest.TestCase):
    profile = 'release'

    def test_names(self):
        # The symbol table only: no lines, the v0 demangling of the names.
        doc = self.stop('target', 1)['ljbt']
        rust = [f for f in doc['frames'] if '::' in (f['name'] or '')]
        self.assertEqual(rust[1]['name'],
                         '<ljbt_plain::Call as ljbt_plain::LuaEntry>::entry')
        self.assertEqual([f['file'] for f in rust], [None] * len(rust))

    # test_rust_functions_folded: call_t is <ljbt_plain::Call as
    # ljbt_plain::LuaEntry>::entry, with no debuginfo its name comes from
    # `info symbol` (ljbt_symname), spaces and all.


class TestRustPlainStrip(PlainMixin, unittest.TestCase):
    profile = 'ljbt-strip'
    named = ('hot',)

    def test_rust_functions_folded(self):
        # No names to match: the Lua C functions stay Lua entries, one per
        # Rust function, and no Lua frame is lost (test_basic, the
        # oracle).
        doc = self.stop('target', 1)['ljbt']
        self.assertEqual(len(unfolded_c(doc)), 2)

    @unittest.expectedFailure
    def test_unnamed_frame_note(self):
        # The note on the unfolded functions says "no C frame of None
        # right above this VM frame: a tail call in C?", though the C frame
        # right above is there, only nameless (ljbt_stitch formats the
        # name None of ljbt_symname).
        doc = self.stop('target', 1)['ljbt']
        info = [i for f in doc['frames'] for i in f['info']]
        self.assertEqual([i for i in info if 'None' in i], [])


class TestRustPlainAbort(PlainMixin, unittest.TestCase):
    '''panic = "abort": the Rust frames keep their unwind tables, but a
    Lua error that unwinds into them hits the landing pad of invoke, which
    calls core::panicking::panic_cannot_unwind: SIGABRT.  The error
    scenario goes last.'''
    profile = 'ljbt-abort'
    scenarios = ('basic', 'error')

    def test_scenarios_done(self):
        self.assertTrue(self.done('basic'))
        self.assertFalse(self.done('error'))
        self.assertIn('panic in a function that cannot unwind', self.out)
        self.assertRegex(self.out, r'received signal SIGABRT')

    def test_error_caught(self):
        self.assertEqual([r for r in self.results
                          if r.get('scenario') == 'error'], [])

    def test_abort(self):
        # At the SIGABRT: the inner band is unwound already (phase 2 of
        # the unwinding, lj_err_unwind_dwarf), the Rust panic runs on top
        # of invoke, the band of ljbt_err below.
        rec = self.stop('end')
        self.assert_clean(rec)
        frames = rec['ljbt']['frames']
        names = [f['name'] for f in frames]
        self.assertIn('core::panicking::panic_cannot_unwind', names)
        k = names.index('core::panicking::panic_cannot_unwind')
        self.assertEqual(role(names[k + 1]), 'invoke')
        rust = shape(rec, self.module)
        self.assertEqual(rust[-3:], [self.R('invoke'), self.R('l_call'),
                                     ('vm', [DEF['err']] + bottom('error'))])
        # gdb unwinds through the Rust frames to the bottom of the fiber
        # stack: coro_init on x86_64, coro_startup on aarch64 (libcoro's
        # entry differs by arch, see LJBT_STACK_BOTTOMS in lj_bt.py).
        self.assertIn(names[-1], ('coro_init', 'coro_startup'))

    test_ffi_from_rust = test_ffi_entry = test_rec = test_rec_counts = None


class TestRustPlainDebugJitOff(RustFrameStopMixin, PlainMixin,
                               unittest.TestCase):
    jit = 'off'
    scenarios = ('basic', 'error', 'ffi')
    test_rec = test_rec_counts = None


# The tnt module. ################################################


class TestRustTnt(RustRun, unittest.TestCase):
    module = 'ljbt_tnt'
    scenarios = ('tlua', 'proc', 'iproto', 'fibers')
    cores = True
    tt_gdb = True

    @classmethod
    def env(cls):
        return {'LJBT_RUST_TNT': os.path.dirname(lib('ljbt-debug',
                                                     'ljbt_tnt'))}

    @classmethod
    def script(cls, probe, gcore):
        tt = 'ljbt-probe-tt {}'.format(C.TT_GDB) if C.TT_GDB else ''
        fibers = 'ljbt-rust-fibers {} {}'.format(
            probe, os.path.join(os.path.dirname(probe), 'results.jsonl')) \
            if C.TT_GDB else ''
        return '\n'.join(s for s in [
            'break lbox_session_exists if $ljbt_arg(L, 0) >= 4000',
            'commands', 'silent', tt,
            'ljbt-rust-probe {} --tag target --all-fibers {}'.format(
                probe, gcore),
            fibers, 'continue', 'end',
            'run'] if s) + '\n'

    def test_scenarios_done(self):
        for s in self.scenarios:
            self.assertTrue(self.done(s), s)
        self.assertRegex(self.out, r'exited normally')

    def test_tlua(self):
        rec = self.stop('target', 5)
        self.assert_clean(rec)
        self.assert_oracle(rec, 5)
        self.assert_rust_names(rec)
        # The tlua function (the generic wrapper) folded into its frame;
        # above it Rust calls Lua in a new lua_State.
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['target'], DEF['from_tlua']]),
            ('R', 'call_global'), ('R', 'wrapper'),
            ('vm', [DEF['tlua']] + bottom('tlua'))])
        doc = rec['ljbt']
        top, under = bands(doc)
        self.assertNotEqual(top['L'], under['L'])
        self.assertEqual(top['L'], doc['L'])
        w = [f for f in doc['frames'] if role(f['name']) == 'wrapper']
        self.assertEqual(w[0]['folded']['func'], w[0]['name'])

    def test_proc_box_func(self):
        rec = self.stop('target', 6)
        self.assert_clean(rec)
        self.assert_oracle(rec, 6)
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['target'], DEF['from_proc']]),
            ('R', 'call_global'), ('R', 'proc'),
            ('vm', [DEF['proc_func']] + bottom('proc'))])
        frames = rec['ljbt']['frames']
        self.assertIn('module_func_call', [f['name'] for f in frames])
        # box.func[...]:call is the C function lbox_func_call.
        f = [f for f in frames if f['name'] == 'lbox_func_call' and
             not f['inline']]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]['folded'] or {}).get('func'),
                         'lbox_func_call')

    def test_proc_iproto(self):
        # A VM entry from C with no Lua frame below it on this C stack: the
        # stored procedure in an iproto fiber.
        rec = self.stop('target', 7)
        self.assert_clean(rec)
        self.assert_oracle(rec, 7)
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['target'], DEF['from_proc']]),
            ('R', 'call_global'), ('R', 'proc')])
        doc = rec['ljbt']
        self.assertEqual(len(doc['states']), 1)
        names = [f['name'] for f in doc['frames']]
        self.assertIn('box_process_call', names)
        self.assertIn('fiber_pool_f', names)

    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_fiber_lua(self):
        # tt-fiber <the Rust fiber that sleeps in Lua> + lj-bt.
        rec = self.stop('fiber-lua')
        self.assert_clean(rec)
        doc = rec['ljbt']
        self.assertEqual(shape(rec, self.module), [
            ('vm', [DEF['fiber_body']]), ('R', 'lua_fiber'),
            ('R', 'trampoline')])
        # fiber.sleep folded into lbox_fiber_sleep, the body at its line.
        lua = [e for e in C.entries(doc) if e['kind'] == 'lua']
        self.assertEqual([(e['fkind'], e['func']) for e in lua],
                         [('c', 'lbox_fiber_sleep'), ('lua', None)])
        self.assertEqual(lua[1]['currentline'], FIBER_SLEEP_LINE)
        f = [f for f in doc['frames'] if f['level'] == lua[0]['cframe_level']]
        self.assertEqual(f[0]['name'], 'lbox_fiber_sleep')
        # Its own lua_State (tlua's lua_state()), not the main one.
        main = self.stop('target', 8)['ljbt']['L']
        self.assertNotEqual(bands(doc)[0]['L'], main)

    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_fiber_rust(self):
        # tt-fiber <the Rust fiber that sleeps in Rust> + lj-bt: its C
        # stack only.
        rec = self.stop('fiber-rust')
        self.assert_clean(rec)
        doc = rec['ljbt']
        self.assertEqual(bands(doc), [])
        self.assertEqual(C.entries(doc), [])
        self.assertEqual(shape(rec, self.module), [
            ('R', 'nap'), ('R', 'rust_fiber'), ('R', 'trampoline')])

    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_all_fibers_runs(self):
        for seq in (7, 8):
            rec = self.stop('target', seq)
            self.assertIn('all_fibers', rec, rec.get('all_fibers_error'))
        names = [x['fiber'] for x in self.stop('target', 8)['all_fibers']]
        self.assertTrue(any('"ljbt-rust-lua"' in n for n in names), names)
        self.assertTrue(any('"ljbt-rust-only"' in n for n in names), names)
        for x in self.stop('target', 8)['all_fibers']:
            if x['bt'] is not None:
                self.assertEqual(C.warnings(x['bt']), [], x['fiber'])

    # lj-bt --all-fibers runs lj-bt only for fibers with a Lua stack of
    # their own (LJBTCommand.all_fibers: `if ljbt_fiber_L(f)`, else "(no
    # Lua stack)", "bt": null): a fiber created from C or Rust has
    # storage.lua.stack == NULL, though it may run Lua in a lua_State of
    # its own (tlua's lua_state(), a C stored procedure) and always has a
    # C stack.  So --all-fibers shows neither, unlike tt-fiber + lj-bt.

    @unittest.expectedFailure
    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_all_fibers_rust_fibers(self):
        allf = dict((x['fiber'], x['bt'])
                    for x in self.stop('target', 8)['all_fibers'])
        for tag in ('fiber-lua', 'fiber-rust'):
            rec = self.stop(tag)
            mine = [b for n, b in allf.items()
                    if n.startswith('fiber {} '.format(rec['fiber']))]
            self.assertEqual(len(mine), 1)
            self.assertIsNotNone(mine[0], tag)
            self.assertEqual(core_shape(mine[0])[0],
                             core_shape(rec['ljbt'])[0])

    @unittest.expectedFailure
    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_all_fibers_running_iproto_fiber(self):
        # At the stop in the iproto fiber, that very fiber.
        rec = self.stop('target', 7)
        running = [x for x in rec['all_fibers'] if x['fiber'] in
                   rec['ljbt']['header']]
        self.assertEqual(len(running), 1, rec['ljbt']['header'])
        self.assertIsNotNone(running[0]['bt'])
        self.assertEqual(core_shape(running[0]['bt'])[0],
                         core_shape(rec['ljbt'])[0])


# LJBT_JIT: the runs are JIT on, plus the debuginfo build JIT off.
if 'on' not in C.jit_modes():
    del TestRustPlainDebugJitOff
    for _cls in (TestRustPlainDebug, TestRustPlainRelease, TestRustPlainStrip,
                 TestRustPlainAbort, TestRustTnt):
        _cls.jit = 'off'
elif 'off' not in C.jit_modes():
    del TestRustPlainDebugJitOff

if __name__ == '__main__':
    unittest.main()
