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
import re
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
shape = C.shape


class CoreMixin(object):
    jit = 'on'

    @classmethod
    def setUpClass(cls):
        cls.wd = C.WorkDir('core')
        prog = gen.Program(cls.jit == 'on', 'main')
        cls.chains = []
        for i, (tr, target) in enumerate(CHAINS):
            ch = gen.Chain(i + 1, tr, target, oracle=False,
                           hot=(i + 1 == HOP))
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

    def test_hop_snapshot_frames(self):
        # The inlined frames of the HOP stop are those of the snapshot lj-bt
        # picks as LuaJIT's own jit.util.tracesnap has it (H.snaps): no
        # more, no less.  HOP calls F7_0 (loop) -> F7_1 -> F7_2 -> FFI,
        # all in one trace, and shows only F7_0, at the line of its call:
        # that is LuaJIT, not lj-bt.  No guard of the trace needs a
        # snapshot inside F7_1 or F7_2 (an exit there may redo the
        # iteration from snapshot #0, at the loop head), and the one that
        # covers the return address of the call (lj_trace_unwind's pick)
        # is taken after it, when both have returned.  No snapshot of the
        # trace has a frame of either, so nothing could show them.
        if self.jit != 'on':
            self.skipTest('JIT is off')
        rec = self.live(HOP)
        m = re.search(r'\[trace (\d+)\].*  snapshot #(\d+)', rec['text'])
        self.assertIsNotNone(m, rec['text'])
        tno, sn = int(m.group(1)), int(m.group(2))
        snaps = [r for r in C.read_jsonl(self.wd.file('snaps.jsonl'))
                 if r['seq'] == HOP]
        self.assertEqual(len(snaps), 1, snaps)
        self.assertEqual(snaps[0]['trace'], tno)
        fr2 = 1 if C.IS_GC64 else 0

        def frames(ents):
            return [e for e in ents if e & 0x10000 and not e & 0x20000 and
                    (e >> 24) & 0xff > fr2]
        theirs = frames(snaps[0]['snaps'][sn])
        tf = next(f for f in rec['ljbt']['frames'] if f['kind'] == 'trace')
        mine = [e for e in tf['lua'] if e.get('inlined')]
        self.assertEqual(len(mine), len(theirs), (mine, theirs))
        self.assertEqual(sum(len(frames(s)) for s in snaps[0]['snaps']), 0,
                         snaps[0])
        # The base frame: F7_0 at the line of the call.
        ch = self.chains[HOP - 1]
        base = next(e for e in C.entries(rec['ljbt'])
                    if 'base frame' in (e.get('note') or ''))
        exp = next(t for t in ch.model if t[0] == 'L' and
                   t[1] == min(ch.deflines))
        self.assertEqual(('L', base['defline'], base['currentline']), exp)

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
