# Layer 5: regressions.
#
# * The tail call heuristic: a positive and negative table (tail.lua).
# * The sysprof crashes in mode C (crash.lua: trigger, json, netbox,
#   sortlong): at the SIGSEGV no Python exception, the C frames of bt,
#   the shape of the crash of that workload (frames from the signal on,
#   Lua lines, the shift of sysprof's stale top_frame), and the same
#   lj-bt on a gcore of the crash.  The crashes are races: each workload
#   gets LJBT_ATTEMPTS runs; no crash in all of them is a skip.

import os
import re
import unittest

import common as C

ATTEMPTS = C.env_int('LJBT_ATTEMPTS', 3)
CRASH_SECS = C.env_int('LJBT_CRASH_SECS', 40)


def heuristics(doc):
    return [e for e in C.entries(doc) if e['kind'] == 'heuristic']


class TailMixin(object):
    jit = 'on'

    @classmethod
    def setUpClass(cls):
        cls.wd = C.WorkDir('tail')
        probe = cls.wd.file('probe.jsonl')
        # box_index_get is armed by the marker box.session.id() after the
        # schema setup (if_not_exists looks spaces up through it too).
        script = C.gdb_prelude() + (
            'break box_index_get\n'
            'commands\nsilent\nljbt-probe {} --tail-chain --text\n'
            'continue\nend\n'
            'disable 1\n'
            'break lbox_session_id\n'
            'commands\nsilent\nenable 1\ncontinue\nend\n'
            'run\n').format(probe)
        os.environ['LJBT_JIT_MODE'] = cls.jit
        C.run_gdb(cls.wd, script, [os.path.join(C.LUA, 'tail.lua')])
        cls.recs = C.read_jsonl(probe)
        with open(os.path.join(C.LUA, 'tail.lua')) as fh:
            cls.src = fh.read().splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.wd.cleanup()

    def line_of(self, text):
        return next(i + 1 for i, s in enumerate(self.src) if text in s)

    def called(self, doc):
        return [h['called'] for h in heuristics(doc)]

    def assert_called(self, rec, expected):
        '''The heuristic lines are expected.  One exception, under JIT: if
        the builtin get_ffi frame comes from a trace snapshot, the
        space_mt.get -> get_ffi tail call may be compiled into the trace
        after that snapshot; the frame slot then still holds space_mt.get,
        which is what the call site says, and there is nothing to see
        (README, known limitations).'''
        doc = rec['ljbt']
        allowed = [expected]
        if self.get_ffi_from_snapshot(doc):
            allowed.append([c for c in expected if c != "method 'get'"])
        self.assertIn(self.called(doc), allowed, rec.get('text'))

    def get_ffi_from_snapshot(self, doc):
        return any(e['kind'] == 'lua' and
                   (e.get('chunk') or '').endswith('schema.lua') and
                   (e.get('inlined') or
                    'base frame, snapshot' in (e.get('note') or ''))
                   for e in C.entries(doc))

    def test_stops(self):
        self.assertEqual(len(self.recs), 4)
        for rec in self.recs:
            self.assertEqual(C.check_doc(rec), [])

    def test_positive_chain(self):
        doc = self.recs[0]['ljbt']
        self.assert_called(self.recs[0], ["method 'get'", "global 'lj_a'"])
        h = heuristics(doc)
        if len(h) == 2:
            self.assertTrue(h[0]['chunk'].endswith('schema.lua'))
        self.assertEqual(h[-1]['defline'], self.line_of('function _G.lj_a'))
        # lj_c runs where lj_a was called from run().
        lua = [e for e in C.entries(doc) if e['kind'] == 'lua' and
               e['fkind'] == 'lua' and e['chunk'].endswith('tail.lua')]
        self.assertEqual([(e['defline'], e['currentline']) for e in lua[:2]],
                         [(self.line_of('function _G.lj_c'),
                           self.line_of('box.space.t:get(x)')),
                          (self.line_of('local function run'),
                           self.line_of('local r1 = lj_a(1)'))])

    def test_tail_chain(self):
        doc = self.recs[0]['ljbt_chain']
        h = heuristics(doc)
        chain = [("global 'lj_b'", True), ("global 'lj_a'", False)]
        if self.get_ffi_from_snapshot(doc) and len(h) == 2:
            self.assertEqual([(e['called'], e['probably']) for e in h],
                             chain)     # See assert_called.
            return
        self.assertEqual([(e['called'], e['probably']) for e in h],
                         [("method 'get'", False)] + chain,
                         self.recs[0].get('text'))
        self.assertRegex(h[0]['text'], r"its CALLT: method 'get' "
                                       r"\(unresolved\)")

    def test_negative(self):
        # A plain call, a local computed in an expression, a call result:
        # only the space_mt.get line, which is a real tail call.
        for rec in self.recs[1:]:
            self.assert_called(rec, ["method 'get'"])


CRASH_LUA = os.path.join(C.LUA, 'crash.lua')


def crash_line(marker):
    with open(CRASH_LUA) as fh:
        return next(i + 1 for i, s in enumerate(fh.read().splitlines())
                    if marker in s)


RX_SYSPROF = re.compile(
    r'!! sysprof walks cur_L from top_frame_info\.top_frame-1 = '
    r'0x[0-9a-f]+, (the top frame is 0x[0-9a-f]+ \(([-+]\d+) slots\)|'
    r'a frame link that is overwritten)')


def frame_tokens(doc):
    '''The frames from the signal frame on (the sysprof handler is above
    it) as kind:name, "=func" for the Lua C function folded in, each with
    a "|" after it; None without a signal frame.'''
    fr = doc['frames']
    k = next((i for i, f in enumerate(fr) if f['kind'] == 'sig'), None)
    if k is None:
        return None
    return ''.join('{}:{}{}|'.format(
        f['kind'], f['name'],
        '=' + f['folded']['func'] if f.get('folded') else '')
        for f in fr[k:])


def lua_text(doc):
    '''The Lua lines of the VM bands, newest first, joined by "|".'''
    return '|'.join(e['text'] for f in doc['frames'] for e in f['lua'])


def from_signal(doc):
    '''C.shape from the signal frame on, levels counted from it, with the
    marks of the frames; the frames above it (name, inline); the notes.
    At the stop of a live process gdb hides the inlined frames whose
    block starts at the pc (as if stepped to their call), on a core it
    shows them: the levels differ by those.'''
    fr = doc['frames']
    k = next(i for i, f in enumerate(fr) if f['kind'] == 'sig')
    below = [(f['level'] - fr[k]['level'], f['kind'], f['name'],
              (f['folded'] or {}).get('text'),
              tuple(e['text'] for e in f['lua']), tuple(f['warnings']))
             for f in fr[k:]]
    return below, [(f['name'], f['inline']) for f in fr[:k]], doc['notes']


class CrashMixin(object):
    '''A crash of one workload of crash.lua.  lj-bt at the SIGSEGV has the
    shape of the workload: FRAMES, the frames from the signal frame on (a
    prefix), and LUA, all the Lua lines, both regexps ({crash} the path
    of crash.lua, {NAME} the line of its marker NAME_LINE or NAME); the
    "!!" of sysprof's stale top_frame: above the top frame by SHIFT
    slots, or (OVERWRITTEN) at it, a link being written.  A workload
    with more than one shape lists them in SHAPES, (FRAMES, LUA, SHIFT,
    OVERWRITTEN) each; the one whose FRAMES match is checked.  A gcore
    taken right there gives the same lj-bt.'''
    what = None
    FRAMES = LUA = None
    SHIFT = (1, 64)
    OVERWRITTEN = True
    SHAPES = None

    def pattern(self, rx):
        lines = dict((m, crash_line(m + '_LINE'))
                     for m in ('TRIGGER', 'JSON', 'SORT', 'MAIN'))
        lines.update((m, crash_line(m)) for m in ('HANDLE_DEF',
                                                  'SERIALIZE_DEF',
                                                  'TRIGGER_DEF'))
        return rx.format(crash=re.escape(CRASH_LUA), **lines)

    def check_shape(self, frames, lua, notes, text):
        '''lj-bt at the crash (frame_tokens, lua_text, notes; text for the
        messages) has one of the shapes of the workload.'''
        shapes = self.SHAPES or [(self.FRAMES, self.LUA, self.SHIFT,
                                  self.OVERWRITTEN)]
        found = [s for s in shapes
                 if re.match('^' + self.pattern(s[0]), frames)]
        self.assertTrue(found, 'no shape of {} has these frames: {}\n{}'
                        .format(self.what, frames, text))
        _, rx, (lo, hi), overwritten = found[0]
        self.assertRegex(lua, '^{}$'.format(self.pattern(rx)), text)
        sysprof = [m for m in (RX_SYSPROF.fullmatch(n) for n in notes) if m]
        self.assertEqual(len(sysprof), 1, notes)
        if sysprof[0].group(2) is None:
            self.assertTrue(overwritten, notes)
        else:
            self.assertGreaterEqual(int(sysprof[0].group(2)), lo, notes)
            if hi is not None:
                self.assertLessEqual(int(sysprof[0].group(2)), hi, notes)

    @unittest.skipUnless(C.HAS_SYSPROF, C.NO_SYSPROF)
    def test_crash(self):
        rec, wd = None, None
        for attempt in range(ATTEMPTS):
            wd = C.WorkDir('crash-' + self.what)
            probe = wd.file('probe.jsonl')
            core = wd.file('core')
            script = C.gdb_prelude() + (
                'handle SIGPROF nostop noprint pass\n'
                'run\n'
                'ljbt-probe {} --text\n'
                'gcore {}\n'
                'kill\n').format(probe, core)
            C.run_gdb(wd, script, [CRASH_LUA, self.what, str(CRASH_SECS)],
                      timeout=CRASH_SECS + 120)
            with open(wd.file('gdb.log')) as fh:
                crashed = re.search(r'received signal SIG(SEGV|BUS|ABRT)',
                                    fh.read())
            recs = C.read_jsonl(probe)
            if crashed and recs:
                rec = recs[0]
                break
            wd.cleanup()
        if rec is None:
            self.skipTest('no crash in {} runs'.format(ATTEMPTS))
        self.assertIsNone(rec['error'], rec['error'])
        doc = rec['ljbt']
        self.assertIsNone(C.cframes_mismatch(doc, rec['bt']))
        frames = frame_tokens(doc)
        self.assertIsNotNone(frames, rec['text'])
        self.check_shape(frames, lua_text(doc), doc['notes'], rec['text'])
        # The core of the crash, no process: the same lj-bt.
        self.assertTrue(os.path.exists(core), 'no core')
        out = wd.file('core-probe.jsonl')
        rc, text = C.run_gdb(wd, C.gdb_prelude() +
                             'ljbt-probe {} --text\n'.format(out), [],
                             core=core)
        os.unlink(core)
        dead = C.read_jsonl(out)
        self.assertEqual(len(dead), 1, text)
        self.assertIsNone(dead[0]['error'], dead[0]['error'])
        self.assertIsNone(C.cframes_mismatch(dead[0]['ljbt'], dead[0]['bt']))
        below, above, notes = from_signal(doc)
        dbelow, dabove, dnotes = from_signal(dead[0]['ljbt'])
        self.assertEqual(dbelow, below)
        self.assertEqual(dnotes, notes)
        extra = len(dabove) - len(above)
        self.assertGreaterEqual(extra, 0, (dabove, above))
        self.assertEqual(dabove[extra:], above)
        self.assertTrue(all(inline for _, inline in dabove[:extra]), dabove)
        wd.cleanup()


class TestCrashTrigger(CrashMixin, unittest.TestCase):
    # In the C code under lbox_replace (pushing the arguments of the
    # trigger, port_c_dump_lua, or the tuple it returns): C frames only
    # below the signal.  cur_L is the fiber of the loop, sysprof's
    # top_frame a frame of another Lua stack, far off.  Not called from a
    # trace, space_mt.replace tail-calls lbox_replace (3.3).
    #
    # Or entering or leaving the trigger (lua_pcall of luaT_call): the VM
    # right below the signal (lj_vm_leave_unw, the end of every return of
    # the VM), the frame of the trigger there or not yet/any more, and
    # sysprof's top_frame that of the trigger on the same stack, a few
    # slots above the top (seen with a non-GC64 build: +3).
    what = 'trigger'
    LUA = (r'(\[lua\] \(tail call from method .replace. = [^|]*\)  '
           r'\[heuristic\]\|)?'
           r'\[lua\] {crash}:{TRIGGER} in function <{crash}:\d+>  '
           r'<called from C: lua_pcall/cpcall>')
    SHAPES = [
        (r'sig:[^|]*\|(c:[^|]*\|)+c:lbox_replace=lbox_replace\|'
         r'vm:lj_BC_FUNCC\|c:lua_pcall\|', LUA, (1000, None), False),
        (r'sig:[^|]*\|vm:lj_(vm|BC)_[^|]*\|c:lua_pcall\|c:luaT_call\|'
         r'(c:[^|]*\|)+c:lbox_replace=lbox_replace\|vm:lj_BC_FUNCC\|'
         r'c:lua_pcall\|',
         r'(\[lua\] {crash}:\d+ in function <{crash}:{TRIGGER_DEF}>  '
         r'<called from C: lua_pcall/cpcall>\|)?' +
         LUA, (1, 64), True),
    ]


class TestCrashShapes(unittest.TestCase):
    '''The shapes of TestCrashTrigger against lj-bt at crashes seen (no
    run, no sysprof): in C under lbox_replace (3.9.0, GC64), and the VM
    leaving the trigger (a non-GC64 build: lj_vm_leave_unw right below
    the signal, sysprof's top_frame 3 slots above the top).'''

    TAIL = ('c:func_adapter_lua_call|c:func_adapter_call|'
            'c:space_run_replace_triggers|c:space_run_replace_triggers|'
            'c:space_on_replace|c:txn_commit_stmt|c:box_process_rw|'
            'c:box_process1|c:box_replace|')
    BOTTOM = ('c:lbox_replace=lbox_replace|vm:lj_BC_FUNCC|c:lua_pcall|'
              'c:luaT_call|c:lua_fiber_run_f|c:fiber_cxx_invoke(fiber_func, '
              'typedef __va_list_tag __va_list_tag *)|c:fiber_loop|'
              'c:coro_init|')
    NOTE = ('!! sysprof walks cur_L from top_frame_info.top_frame-1 = {}, '
            'the top frame is {} ({} slots)')

    def check(self, frames, note):
        line = crash_line('TRIGGER_LINE')
        # The fiber function of the loop, 3 lines above its replace.
        lua = ('[lua] {0}:{1} in function <{0}:{2}>  <called from C: '
               'lua_pcall/cpcall>'.format(CRASH_LUA, line, line - 3))
        TestCrashTrigger('test_crash').check_shape(frames, lua, [note],
                                                   frames)

    def test_in_c(self):
        self.check('sig:__restore_rt|c:lj_tab_newkey|c:lj_tab_set|'
                   'c:lj_tab_resize|c:rehashtab|c:lj_tab_newkey|c:lj_tab_set|'
                   'c:lj_cdata_setfin|c:luaL_setcdatagc|c:luaT_pushtuple|'
                   'c:port_c_dump_lua|c:port_dump_lua|' + self.TAIL +
                   'c:lbox_replace|' + self.BOTTOM,
                   self.NOTE.format('0x7ffff79485a8', '0x7ffff6374680',
                                    '+2861029'))

    def test_leaving_the_trigger(self):
        self.check('sig:__restore_rt|vm:lj_vm_leave_unw|c:lua_pcall|'
                   'c:luaT_call|' + self.TAIL + self.BOTTOM,
                   self.NOTE.format('0x40621a58', '0x40621a40', '+3'))


class TestCrashJson(CrashMixin, unittest.TestCase):
    # Entering or leaving the __serialize of a row (lua_pcall of the
    # encoder): the VM right below the signal, the frame of __serialize
    # there or not yet/any more.
    what = 'json'
    FRAMES = (r'sig:[^|]*\|vm:lj_(vm|BC)_[^|]*\|c:lua_pcall\|'
              r'c:lua_field_try_serialize\|(c:[^|]*\|)*'
              r'c:json_encode=json_encode\|vm:lj_BC_FUNCC\|c:lua_pcall\|')
    LUA = (r'(\[lua\] {crash}:\d+ in function <{crash}:{SERIALIZE_DEF}>'
           r'  <called from C: lua_pcall/cpcall>\|)?'
           r'\[lua\] {crash}:{JSON} in function <{crash}:\d+>  '
           r'<called from C: lua_pcall/cpcall>')


class TestCrashNetbox(CrashMixin, unittest.TestCase):
    # Entering or leaving the function of an iproto call (lua_pcall of
    # execute_lua_call, its lua_call of handle).
    what = 'netbox'
    FRAMES = (r'sig:[^|]*\|vm:lj_(vm|BC)_[^|]*\|'
              r'(c:execute_lua_call=execute_lua_call\|vm:lj_BC_FUNCC\|)?'
              r'c:lua_pcall\|c:luaT_call\|c:box_process_lua\|'
              r'c:box_lua_call\|c:box_process_call\|c:tx_process_call\|')
    LUA = (r'(\[lua\] {crash}:\d+ in function <{crash}:{HANDLE_DEF}>  '
           r'<called from C: lua_call>)?')


class TestCrashSortlong(CrashMixin, unittest.TestCase):
    # Entering or leaving the comparator (lua_call of sort_comp).
    what = 'sortlong'
    FRAMES = (r'sig:[^|]*\|vm:lj_(vm|BC)_[^|]*\|c:sort_comp\|'
              r'(c:(sort_comp|auxsort)\|)*c:lj_cf_table_sort=table\.sort\|'
              r'vm:lj_BC_FUNCC\|c:lua_pcall\|c:luaT_call\|c:lua_main\|')
    LUA = (r'(\[lua\] {crash}:{SORT} in function <{crash}:{SORT}>  '
           r'<called from C: lua_call>\|)?'
           r'\[lua\] {crash}:{SORT} in function <{crash}:\d+>\|'
           r'\[lua\] {crash}:{MAIN} in main chunk  '
           r'<called from C: lua_pcall/cpcall>')


for _jit in C.jit_modes():
    _name = 'TestTail_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (TailMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
