# Layer 5: regressions.
#
# * The tail call heuristic: a positive and negative table (tail.lua).
# * The sysprof crashes in mode C (crash.lua: trigger, json, netbox,
#   sortlong): at the SIGSEGV no Python exception, a "!!" naming the
#   inconsistency, and the right Lua frame.  The crashes are races: each
#   workload gets LJBT_ATTEMPTS runs; no crash in all of them is a skip.

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
            'commands\nsilent\nljbt-probe {} --tail-chain\ncontinue\nend\n'
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

    def test_stops(self):
        self.assertEqual(len(self.recs), 4)
        for rec in self.recs:
            self.assertEqual(C.check_doc(rec), [])

    def test_positive_chain(self):
        doc = self.recs[0]['ljbt']
        self.assertEqual(self.called(doc), ["method 'get'", "global 'lj_a'"])
        h = heuristics(doc)
        self.assertTrue(h[0]['chunk'].endswith('schema.lua'))
        self.assertEqual(h[1]['defline'], self.line_of('function _G.lj_a'))
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
        self.assertEqual([(e['called'], e['probably']) for e in h],
                         [("method 'get'", False), ("global 'lj_b'", True),
                          ("global 'lj_a'", False)])
        self.assertRegex(h[0]['text'], r"its CALLT: method 'get' "
                                       r"\(unresolved\)")

    def test_negative(self):
        # A plain call, a local computed in an expression, a call result:
        # only the space_mt.get line, which is a real tail call.
        for rec in self.recs[1:]:
            self.assertEqual(self.called(rec['ljbt']), ["method 'get'"])


class CrashMixin(object):
    what = None

    def test_crash(self):
        rec, wd = None, None
        for attempt in range(ATTEMPTS):
            wd = C.WorkDir('crash-' + self.what)
            probe = wd.file('probe.jsonl')
            script = C.gdb_prelude() + (
                'handle SIGPROF nostop noprint pass\n'
                'run\n'
                'ljbt-probe {}\n'
                'kill\n').format(probe)
            C.run_gdb(wd, script, [os.path.join(C.LUA, 'crash.lua'),
                                   self.what, str(CRASH_SECS)],
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
        w = C.warnings(doc)
        self.assertTrue(any(re.search(
            r'sysprof walks .*(slots\)|overwritten)|owns no Lua frame|'
            r'is not a cframe|broken frame link', x) for x in w), w)
        self.check_frames(doc)
        wd.cleanup()

    def check_frames(self, doc):
        pass


class TestCrashTrigger(CrashMixin, unittest.TestCase):
    what = 'trigger'

    def check_frames(self, doc):
        with open(os.path.join(C.LUA, 'crash.lua')) as fh:
            line = next(i + 1 for i, s in enumerate(fh.read().splitlines())
                        if 'TRIGGER_LINE' in s)
        lua = [(e['chunk'], e['currentline']) for e in C.entries(doc)
               if e['kind'] == 'lua' and e['fkind'] == 'lua']
        self.assertIn(line, [ln for ch, ln in lua
                             if ch and ch.endswith('crash.lua')], lua)


class TestCrashJson(CrashMixin, unittest.TestCase):
    what = 'json'


class TestCrashNetbox(CrashMixin, unittest.TestCase):
    what = 'netbox'


class TestCrashSortlong(CrashMixin, unittest.TestCase):
    what = 'sortlong'


for _jit in C.jit_modes():
    _name = 'TestTail_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (TailMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
