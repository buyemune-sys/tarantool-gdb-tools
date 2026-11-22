# Layer 4: post-mortem.  At some layer 1 breakpoints the probe runs
# `gcore`; then gdb opens each core file without a process and
# `lj-bt --json` must give what it gave on the live process.

import os
import unittest

import common as C
import gen

CHAINS = [['call'], ['resume', 'call'], ['sort', 'tail'], ['fiber', 'call'],
          ['jit', 'call'], ['xerror']]


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
        for i, tr in enumerate(CHAINS):
            ch = gen.Chain(i + 1, tr, 'ffi' if i % 2 else 'c', oracle=False)
            prog.add(ch)
            cls.chains.append(ch)
        seqs = ','.join(str(ch.seq) for ch in cls.chains)
        cls.stops, _, cls.failures, cls.out = C.run_chains(
            cls.wd, prog, '--gcore-dir {} --gcore-seqs {}'.format(
                cls.wd.path, seqs))

    @classmethod
    def tearDownClass(cls):
        cls.wd.cleanup()

    def test_core_equals_live(self):
        for ch in self.chains:
            with self.subTest(seq=ch.seq, chain=ch.transitions):
                recs = self.stops.get(ch.seq, [])
                self.assertEqual(len(recs), 1)
                live = recs[0]
                self.assertIn('core', live, live.get('core_error'))
                out = self.wd.file('core-probe.{}.jsonl'.format(ch.seq))
                rc, text = C.run_gdb(
                    self.wd, C.gdb_prelude() + 'ljbt-probe {}\n'.format(out),
                    [], core=live['core'])
                os.unlink(live['core'])
                dead = C.read_jsonl(out)
                self.assertEqual(len(dead), 1, text)
                self.assertIsNone(dead[0]['error'], dead[0]['error'])
                self.assertEqual(shape(dead[0]['ljbt']),
                                 shape(live['ljbt']))
                self.assertIsNone(C.cframes_mismatch(dead[0]['ljbt'],
                                                     dead[0]['bt']))


for _jit in C.jit_modes():
    _name = 'TestCore_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (CoreMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
