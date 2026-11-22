# Layer 4: post-mortem.  At some layer 1 breakpoints the probe runs
# `gcore`; then gdb opens each core file without a process and
# `lj-bt --json` must give what it gave on the live process.  The first
# command on a core is a plain `bt`: no stop event has built the state of
# lj-bt, the trace unwinder builds it when gdb first unwinds (with JIT on,
# HOP stops in machine code, behind the FFI call of a trace).  At the
# same stop on the live process the probe goes to another thread and
# back (`thread N`), then `bt`: in 3.7+ the other thread has no G, the
# unwinder builds the state of the stopped one again.

import os
import unittest

import common as C
import gen

# (transitions, target)
CHAINS = [(['call'], 'c'), (['resume', 'call'], 'ffi'),
          (['sort', 'tail'], 'c'), (['fiber', 'call'], 'ffi'),
          (['jit', 'call'], 'c'), (['xerror'], 'ffi'),
          (['jit', 'call'], 'ffi')]
HOP = 7                 # The seq of the JIT chain with the FFI target.

# The first command on a core, before the probe.
BT_FIRST = 'echo ==bt-first\\n\nbt\necho ==bt-end\\n\n'


def shape(doc):
    '''What must not depend on live vs core.'''
    res = []
    for f in doc['frames']:
        res.append((f['level'], f['kind'], f['name'],
                    (f['folded'] or {}).get('text'),
                    tuple(e['text'] for e in f['lua'])))
    return res, [n for n in doc['notes']]


class CoreMixin(object):
    jit = 'on'

    @classmethod
    def setUpClass(cls):
        cls.wd = C.WorkDir('core')
        prog = gen.Program(cls.jit == 'on', 'main')
        cls.chains = []
        for i, (tr, target) in enumerate(CHAINS):
            ch = gen.Chain(i + 1, tr, target, oracle=False)
            prog.add(ch)
            cls.chains.append(ch)
        seqs = ','.join(str(ch.seq) for ch in cls.chains)
        cls.stops, _, cls.failures, cls.out = C.run_chains(
            cls.wd, prog, '--text --gcore-dir {} --gcore-seqs {} '
            '--thread-hop {}'.format(cls.wd.path, seqs, HOP))

    @classmethod
    def tearDownClass(cls):
        cls.wd.cleanup()

    def live(self, seq):
        recs = self.stops.get(seq, [])
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0]['error'], recs[0]['error'])
        return recs[0]

    def in_trace(self, rec):
        return any(f['kind'] == 'trace' for f in rec['ljbt']['frames'])

    def test_hop_stops_in_a_trace(self):
        # What the HOP tests rely on.
        rec = self.live(HOP)
        self.assertEqual(self.in_trace(rec), self.jit == 'on')

    def test_core_equals_live(self):
        for ch in self.chains:
            with self.subTest(seq=ch.seq, chain=ch.transitions):
                live = self.live(ch.seq)
                self.assertIn('core', live, live.get('core_error'))
                out = self.wd.file('core-probe.{}.jsonl'.format(ch.seq))
                rc, text = C.run_gdb(
                    self.wd, C.gdb_prelude() + BT_FIRST +
                    'ljbt-probe {} --text\n'.format(out), [],
                    core=live['core'])
                os.unlink(live['core'])
                dead = C.read_jsonl(out)
                self.assertEqual(len(dead), 1, text)
                self.assertIsNone(dead[0]['error'], dead[0]['error'])
                self.assertEqual(shape(dead[0]['ljbt']),
                                 shape(live['ljbt']))
                self.assertIsNone(C.cframes_mismatch(dead[0]['ljbt'],
                                                     dead[0]['bt']))
                # The first bt on the core: the Lua frames of lj-bt, no
                # mark (the trace unwinder did not wait for a stop event).
                first = text.split('==bt-first')[1].split('==bt-end')[0]
                self.assertEqual(C.lua_lines(first),
                                 C.lua_lines(dead[0]['text']))
                self.assertTrue(C.lua_lines(first), first)
                self.assertEqual(C.mark_lines(first), [])

    def test_bt_after_thread_switch(self):
        # `thread N`, `bt` there, back, `bt`: the Lua frames of lj-bt.
        rec = self.live(HOP)
        self.assertIn('thread_hop', rec, rec.get('thread_hop_error'))
        hop = rec['thread_hop']
        self.assertIsNotNone(hop, 'no other thread')
        self.assertEqual(C.lua_lines(hop['bt']), C.lua_lines(hop['text']))
        self.assertTrue(C.lua_lines(hop['bt']), hop['bt'])
        self.assertEqual(C.mark_lines(hop['bt']), [])


for _jit in C.jit_modes():
    _name = 'TestCore_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (CoreMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
