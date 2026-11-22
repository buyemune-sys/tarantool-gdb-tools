# Layer 1: lj-bt against LuaJIT's own view of the stack.
#
# Fixed chains covering every transition of gen.py.  Right before the
# target C function the chain writes its levels from debug.getinfo (the
# oracle); at the breakpoint `lj-bt --json` must give the same Lua frames
# in the same order with the same lines, the C frames of `bt -no-filters`,
# each Lua frame once, as many frames as lj-stack, and no "!!".

import re
import unittest

import common as C
import gen

FIXED = [
    ['call'],
    ['tail', 'tail', 'call'],
    ['pcall', 'tail'],
    ['xpcall'],
    ['resume', 'call'],
    ['wrap', 'tail'],
    ['sort'],
    ['gsub', 'pcall'],
    ['index'],
    ['callmt', 'tail'],
    ['add'],
    ['fiber', 'call'],
    ['xerror'],
    ['jit', 'call'],
    ['jit', 'tail', 'call'],
    ['call', 'resume', 'sort', 'fiber', 'pcall', 'tail'],
]


def fixed_program(jit, driver, first_seq=1):
    prog = gen.Program(jit == 'on', driver)
    chains = []
    for i, tr in enumerate(FIXED):
        ch = gen.Chain(first_seq + i, tr, 'c' if i % 2 == 0 else 'ffi',
                       oracle=True)
        prog.add(ch)
        chains.append(ch)
    return prog, chains


class OracleMixin(object):
    jit = 'off'
    driver = 'main'

    @classmethod
    def setUpClass(cls):
        cls.wd = C.WorkDir('oracle')
        prog, cls.chains = fixed_program(cls.jit, cls.driver)
        cls.stops, cls.oracle, cls.failures, cls.out = C.run_chains(
            cls.wd, prog, '--filter --text --all-fibers')

    @classmethod
    def tearDownClass(cls):
        cls.wd.cleanup()

    def test_chains_ran(self):
        self.assertEqual(self.failures, [])
        self.assertNotIn('<timeout>', self.out)

    def test_chains(self):
        for ch in self.chains:
            with self.subTest(seq=ch.seq, chain=ch.transitions,
                              target=ch.target):
                probs = C.check_chain(ch, self.stops.get(ch.seq, []),
                                      self.oracle, gen.tokens,
                                      gen.oracle_tokens)
                self.assertEqual(probs, [], '\n'.join(probs))

    def test_text_output(self):
        # A few regexps on the text forms; the rest is checked on JSON.
        rec = self.stops[1][0]
        self.assertRegex(rec['text'], r'(?m)^lj-bt: L=0x[0-9a-f]+ ')
        self.assertRegex(rec['text'],
                         r'(?m)^#\d+\s+lj_BC_FUNCC \(\)  \[VM entry L=')
        self.assertRegex(rec['text'], r'(?m)^#\d+\.\d+\s+\[lua\] \S+:\d+ in ')
        self.assertRegex(rec['text'], r'<- C function lbox_session_exists')
        self.assertNotIn('lj-bt frame filter:', rec['bt_filter'])
        self.assertRegex(rec['bt_filter'], r'(?m)^    \[lua\] \S+:\d+ in ')

    def test_tail_heuristic(self):
        # ['tail', 'tail', 'call']: F2_0 and F2_1 are gone; the CALL in D
        # still says F2_0.
        doc = self.stops[2][0]['ljbt']
        h = [e for e in C.entries(doc) if e['kind'] == 'heuristic']
        self.assertEqual(len(h), 1, h)
        self.assertRegex(h[0]['called'], r"^global 'F2_0'$")

    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_sleeping_parent_fiber(self):
        # ['fiber', 'call']: the creator of the fiber sleeps in ch:get()
        # inside F12_0; lj-bt of that fiber (tt-fiber) shows it.
        rec = self.stops[12][0]
        self.assertIn('all_fibers', rec, rec.get('all_fibers_error'))
        found = False
        for fb in rec['all_fibers']:
            if fb['bt'] is None:
                continue
            for e in C.entries(fb['bt']):
                if e.get('defline') is not None and e['kind'] == 'lua' and \
                        e['chunk'] and e['chunk'].endswith('chains.lua') and \
                        re.search(r'F12_0', self.chain_src(e['defline'])):
                    found = True
        self.assertTrue(found, 'F12_0 not found in any fiber')

    def chain_src(self, line):
        with open(self.wd.file('chains.lua')) as fh:
            return fh.read().splitlines()[line - 1]


for _jit in C.jit_modes():
    for _driver in ('main', 'fiber'):
        _name = 'TestOracle_jit_{}_{}'.format(_jit, _driver)
        globals()[_name] = type(_name, (OracleMixin, unittest.TestCase),
                                {'jit': _jit, 'driver': _driver})

if __name__ == '__main__':
    unittest.main()
