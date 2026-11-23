# The window layer: lj-bt at every instruction of the windows of the VM,
# and at stops of a timer without sysprof.
#
# * Windows: in a chain with window=True (gen.py) F_0 calls a mark C
#   function right before its step; window_gdb.py goes from the mark to
#   the chain's target one instruction at a time through the code of the
#   VM (C functions it calls run by `finish`): the return of a C
#   function, the bytecodes of the step, its frames being set up (a Lua
#   call, a tail call, pcall, a coroutine resume, a metamethod, a call
#   from C, ...).  At every instruction: no Python exception, the C
#   frames of `bt -no-filters`, the invariants of check_doc, and either a
#   "!!" mark or the Lua frames a prefix of a stack the chain goes
#   through (the newest frame at any line): of the model, of the creator
#   of a fiber, of F_0 at its step with the mark on top.  And once a
#   frame of the model is shown it stays, unless lj-bt marks a stop or
#   says the top may be stale (no frame in the registers of the VM).
# * Timer stops: the workload of the stops layer (load.lua) with SIGPROF
#   from setitimer instead of sysprof, so they run where LuaJIT has no
#   sysprof (arm64); lj-bt when the signal arrives and, after a `stepi`
#   into its handler, under the frame of the signal.  The checks of the
#   stops layer, and under the signal the same as at it.
# Stops breaking that are kept in violations.jsonl of the (kept) work
# directory.

import json
import os
import unittest

import common as C
import gen
import test_stops as S

WINDOW_GDB = os.path.join(C.TESTS, 'window_gdb.py')
# Instructions probed per chain at most (a window is 20..200 long).
MAXSTEPS = 1000
# Each a different path through the VM (coroutine.wrap takes the one of
# resume, __call of callmt that of tailmt).
WINDOWS = [['call'], ['tail', 'call'], ['pcall'], ['xpcall'], ['resume'],
           ['index'], ['add'], ['vararg'], ['tailiter'], ['tailmt'],
           ['sort'], ['qsort'], ['load'], ['fiber']]
MARK = ('B', 'lbox_session_exists')

SCRIPT = '''\
break lbox_session_exists if $ljbt_arg(L, 0) >= 2000 && $ljbt_arg(L, 0) < 3000
break lbox_session_exists if $ljbt_arg(L, 0) >= 4000
run
ljbt-window {probe} {seqs} {steps}
kill
'''

TIMER_SCRIPT = '''\
handle SIGPROF stop pass
run
python
import gdb
for _ in range({n}):
    try:
        gdb.execute('ljbt-probe {probe} --tag signal')
        gdb.execute('stepi', False, True)
        gdb.execute('ljbt-probe {probe} --tag handler')
        gdb.execute('continue', False, True)
    except gdb.error:
        break
end
kill
'''


def is_prefix(toks, stack):
    '''toks is a prefix of stack, its newest frame at any line.'''
    n = len(toks)
    return 0 < n <= len(stack) and toks[:-1] == stack[:n - 1] and \
        toks[-1][:2] == stack[n - 1][:2]


def stacks(ch):
    '''The stacks a window of the chain goes through, oldest frame
    first: F_0 at its step under the mark, frames of a step that are
    gone at the stop (the head of a tail run before its tail call, the
    lookup of C.qsort), the creators of its fibers, the model.'''
    return [ch.bottom + [ch.first, MARK]] + ch.transient + ch.parents + \
        [ch.model]


def window_problems(ch, rec):
    if rec.get('error'):
        return ['python exception:\n' + rec['error']]
    doc = rec['ljbt']
    probs = []
    m = C.cframes_mismatch(doc, rec['bt'])
    if m:
        probs.append('C frames: ' + m)
    probs += [p for p in C.check_doc(rec, allow_warnings=True)
              if not p.startswith('C frames')]
    if not C.warnings(doc):
        if not S.outer_found(doc):
            probs.append('outer Lua frame not found, no "!!"')
        toks = gen.tokens(C.entries(doc))
        if not any(is_prefix(toks, s) for s in stacks(ch)):
            probs.append('no "!!", and not a stack of the chain:\n'
                         '  lj-bt {}\n  {}'.format(toks, '\n  '.join(
                             str(s) for s in stacks(ch))))
    return probs


def stale(doc):
    '''lj-bt says newer frames may be missing: stopped in the VM with no
    frame in its registers.'''
    return any('with no frame in its registers' in n for n in doc['notes'])


def progress(ch, toks):
    '''How far down the chain a window stop is: the number of its frames
    in the model (or in the stack of the creator of a fiber); the mark on
    top of F_0 comes before F_0 alone, the frame of a step that goes away
    before the next frame of the model.  None for another stack.'''
    mark = ch.bottom + [ch.first, MARK]
    if toks == mark:
        return len(ch.bottom) + 0.5
    if len(toks) < len(mark) and is_prefix(toks, mark):
        return len(toks)            # F_0 at its step (a tail call next).
    if any(len(toks) == len(t) and is_prefix(toks, t)
           for t in ch.transient):
        return len(toks) - 0.5
    if any(is_prefix(toks, s) for s in ch.parents + [ch.model]):
        return len(toks)
    return None


def run_by_vm(ch):
    '''The progress at which each Lua frame the VM runs in the window is
    the newest: F_0, then those of the model down to the first one
    called from C (it runs inside a `finish`, unprobed).'''
    res = {len(ch.bottom) + 1}
    if ch.parents:
        return res                  # The model is of another fiber.
    for j in range(len(ch.bottom), len(ch.model)):
        a = ch.attrs[j]
        if a['ftype'] == 'C' or 'FFI callback' in (a['how'] or []) or \
                'called from C: lua_pcall/cpcall' in (a['how'] or []):
            break
        if ch.model[j][0] == 'L':
            res.add(j + 1)
    return res


def depth_problems(ch, recs):
    '''The window goes down the chain: once a frame of it is shown it
    stays to the target, the mark goes before the step, and each Lua
    frame the VM runs is the newest at some stop.  A stop further up the
    chain than one before it is a lost frame, unless it has a "!!" or
    says its top may be stale; those do not count for being shown.'''
    probs, deepest, seen = [], 0, set()
    for rec in recs:
        doc = rec.get('ljbt')
        if not doc or C.warnings(doc) or stale(doc):
            continue
        toks = gen.tokens(C.entries(doc))
        p = progress(ch, toks)
        if p is None:
            continue
        if p < deepest:
            probs.append('step {}: back up the chain ({} after {}): {}'
                         .format(rec['tag'], p, deepest, toks))
        deepest = max(deepest, p)
        seen.add(p)
    missing = sorted(run_by_vm(ch) - seen)
    if missing:
        probs.append('never the newest at a stop: {}'.format(
            [(ch.bottom + [ch.first] if p == len(ch.bottom) + 1
              else ch.model)[p - 1] for p in missing]))
    return probs


def frame_shape(f):
    return (f['kind'], f['name'], (f['folded'] or {}).get('text'),
            tuple(e['text'] for e in f['lua']), tuple(f['warnings']))


def pair_problems(at, inh):
    '''The stop in the handler interrupts the state of the stop at the
    signal: from under the frame of the signal on, lj-bt shows what it
    showed at the signal.  gdb hides inlined frames whose block starts at
    the pc of a stop, not under a signal frame: there may be more.'''
    if not at.get('ljbt') or not inh.get('ljbt'):
        return []           # violations() has the exception.
    fa, fh = at['ljbt']['frames'], inh['ljbt']['frames']
    k = next((i for i, f in enumerate(fh[:3]) if f['kind'] == 'sig'), None)
    if k is None:
        return ['no frame of the signal under the handler']
    below = fh[k + 1:]
    extra = len(below) - len(fa)
    probs = []
    if extra < 0 or not all(f['inline'] for f in below[:extra]) or \
            [frame_shape(f) for f in below[extra:]] != \
            [frame_shape(f) for f in fa]:
        probs.append('under the signal not the stop at it:\n  {}\n  {}'
                     .format(inh['text'] if 'text' in inh else
                             [frame_shape(f) for f in below],
                             [frame_shape(f) for f in fa]))
    if at['ljbt']['notes'] != inh['ljbt']['notes']:
        probs.append('notes differ: {} vs {}'.format(
            inh['ljbt']['notes'], at['ljbt']['notes']))
    return probs


def dump(wd, bad):
    with open(wd.file('violations.jsonl'), 'w') as fh:
        for b in bad:
            fh.write(json.dumps(b) + '\n')


class WindowMixin(object):
    jit = 'off'

    def test_windows(self):
        wd = C.WorkDir('window')
        prog = gen.Program(self.jit == 'on', 'main')
        chains = {}
        for i, tr in enumerate(WINDOWS):
            ch = gen.Chain(i + 1, tr, 'c', oracle=False, window=True)
            prog.add(ch)
            chains[ch.seq] = ch
        src = wd.write('chains.lua', prog.render())
        probe = wd.file('probe.jsonl')
        script = C.gdb_prelude() + 'source {}\n'.format(WINDOW_GDB) + \
            SCRIPT.format(probe=probe, seqs=','.join(
                str(s) for s in sorted(chains)), steps=MAXSTEPS)
        rc, out = C.run_gdb(wd, script, [src])
        recs = C.read_jsonl(probe)
        ends = dict((r['window'], r) for r in recs if 'window' in r)
        stops = [r for r in recs if 'window' not in r]
        bad, classes = [], {}
        for rec in stops:
            ch = chains[rec['seq']]
            k = S.stop_class(rec.get('ljbt'))
            classes[k] = classes.get(k, 0) + 1
            v = window_problems(ch, rec)
            if v:
                bad.append(dict(rec, chain=ch.transitions, violations=v))
        if bad:
            dump(wd, bad)
        self.assertNotIn('<timeout>', out)
        for seq, ch in sorted(chains.items()):
            with self.subTest(seq=seq, chain=ch.transitions):
                end = ends.get(seq)
                self.assertIsNotNone(end, 'the window did not end')
                self.assertTrue(end['reached'], end)
                self.assertGreaterEqual(end['steps'], 10, end)
                self.assertEqual(depth_problems(ch, [
                    r for r in stops if r['seq'] == seq]), [])
        self.assertEqual([], [(b['seq'], b['tag'], b['chain'],
                               b['violations']) for b in bad],
                         'dumps: ' + wd.file('violations.jsonl'))
        # Not vacuous: clean stops in bytecode handlers and elsewhere in
        # the VM.
        for k in ('bc', 'vm'):
            self.assertGreater(classes.get((k, 'clean'), 0), 0,
                               'no clean stop in {}: {}'.format(k, classes))
        if not bad:
            wd.cleanup()


class TimerMixin(object):
    jit = 'on'

    def test_timer_stops(self):
        wd = C.WorkDir('timer')
        probe = wd.file('probe.jsonl')
        n = S.STOPS
        script = C.gdb_prelude() + TIMER_SCRIPT.format(n=n, probe=probe)
        os.environ['LJBT_JIT_MODE'] = self.jit
        os.environ['LJBT_TIMER'] = '1'
        try:
            C.run_gdb(wd, script, [S.LOAD], timeout=max(230, n * 2))
        finally:
            del os.environ['LJBT_TIMER']
        recs = C.read_jsonl(probe)
        at = [r for r in recs if r.get('tag') == 'signal']
        inh = [r for r in recs if r.get('tag') == 'handler']
        self.assertGreaterEqual(len(at), n * 0.9, 'only {} stops'.format(
            len(at)))
        bad, classes = [], {}
        for i, rec in enumerate(recs):
            if rec.get('tag') == 'signal':
                k = S.stop_class(rec.get('ljbt'))
                classes[k] = classes.get(k, 0) + 1
            v = S.violations(rec)
            if rec.get('tag') == 'handler' and i > 0 and \
                    recs[i - 1].get('tag') == 'signal':
                v += pair_problems(recs[i - 1], rec)
            if v:
                bad.append(dict(rec, stop=i, violations=v))
        if bad:
            dump(wd, bad)
        self.assertEqual([], [(b['stop'], b['tag'], b['violations'])
                              for b in bad],
                         'dumps: ' + wd.file('violations.jsonl'))
        # In the handler: the frame of the signal right under it, and Lua
        # frames under that.
        sig = [r for r in inh if r.get('ljbt') and any(
            f['kind'] == 'sig' for f in r['ljbt']['frames'][:3])]
        self.assertGreaterEqual(len(sig), len(inh) * 0.9,
                                '{} of {}'.format(len(sig), len(inh)))
        self.assertTrue(any(C.entries(r['ljbt']) for r in sig),
                        'no Lua frames under a signal')
        if len(at) >= S.MIN_CLASS_STOPS:
            for k in S.CLASSES[self.jit]:
                self.assertGreater(classes.get((k, 'clean'), 0), 0,
                                   'no clean stop in {}: {}'.format(
                                       k, classes))
        if not bad:
            wd.cleanup()


# The chains of the windows run once, in the interpreter, whatever the
# JIT: one mode is enough.
_name = 'TestWindow_jit_{}'.format(C.jit_modes()[-1])
globals()[_name] = type(_name, (WindowMixin, unittest.TestCase),
                        {'jit': C.jit_modes()[-1]})
for _jit in C.jit_modes():
    _name = 'TestTimer_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (TimerMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
