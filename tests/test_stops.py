# Layer 3: stops at random points of a mixed workload (SIGPROF of
# sysprof in mode D, `handle SIGPROF stop`).  At every stop: no Python
# exception, the C frames of `bt -no-filters`, and either a consistent
# picture (the outer Lua frame - a main chunk or a function called from
# C - found, lj-stack agrees) or a "!!" mark.  Stops breaking that are
# kept in violations.jsonl of the (kept) work directory.

import json
import os
import unittest

import common as C

STOPS = C.env_int('LJBT_STOPS', 500)

SCRIPT = '''\
handle SIGPROF stop pass
run
python
import gdb
for _ in range({n}):
    try:
        gdb.execute('ljbt-probe {probe}')
        gdb.execute('continue', False, True)
    except gdb.error:
        break
end
kill
'''


def outer_found(doc):
    '''The oldest Lua frame of the stopped C stack is where Lua was
    entered from C: a function called by lua_call/lua_pcall or a main
    chunk.'''
    es = [e for e in C.entries(doc) if e['kind'] == 'lua']
    if not es:
        return True             # No Lua on this stack.
    last = es[-1]
    how = ' '.join(last.get('how') or [])
    return 'called from C' in how or last.get('defline') == 0 or \
        last.get('ftype') in ('C', 'CP')


def violations(rec):
    if rec.get('error'):
        return ['python exception:\n' + rec['error']]
    probs = []
    doc = rec['ljbt']
    m = C.cframes_mismatch(doc, rec['bt'])
    if m:
        probs.append('C frames: ' + m)
    # A mark excuses what it marks, an inconsistent Lua stack: the outer
    # frame may be out of reach.  The rest holds on any stack: each frame
    # link once, synthetic frames after traces, the frames of the states
    # lj-bt calls consistent (check_doc skips the broken ones).
    if not C.warnings(doc) and not outer_found(doc):
        probs.append('outer Lua frame not found, no "!!"')
    probs += [p for p in C.check_doc(rec, allow_warnings=True)
              if not p.startswith('C frames')]
    return probs


def stop_class(doc):
    '''Where a stop is: 'bc' in a bytecode handler, 'trace' in machine
    code of a trace, 'c' in C, 'vm' elsewhere in the VM; with a mark or
    clean.'''
    f = doc['frames'][0] if doc and doc['frames'] else None
    if f is None:
        return None
    if f['kind'] == 'trace':
        k = 'trace'
    elif f['kind'] in ('vm', 'vm?'):
        k = 'bc' if (f['name'] or '').startswith('lj_BC_') else 'vm'
    else:
        k = f['kind']
    return k, 'marked' if C.warnings(doc) else 'clean'


# Clean stops of each class a run must have, by JIT mode, once it has
# MIN_CLASS_STOPS stops or more (of 100 stops on x86_64 3.9: JIT off ~17
# in bytecode handlers, ~73 in C; JIT on ~8 in traces, ~75 in C, ~3 in
# handlers - too few to require).
MIN_CLASS_STOPS = 100
CLASSES = {'off': ('bc', 'c'), 'on': ('trace', 'c')}


class StopsMixin(object):
    jit = 'on'

    @unittest.skipUnless(C.HAS_SYSPROF, C.NO_SYSPROF)
    def test_random_stops(self):
        wd = C.WorkDir('stops')
        probe = wd.file('probe.jsonl')
        script = C.gdb_prelude() + SCRIPT.format(n=STOPS, probe=probe)
        os.environ['LJBT_JIT_MODE'] = self.jit
        C.run_gdb(wd, script, [os.path.join(C.LUA, 'load.lua')],
                  timeout=max(230, STOPS))
        recs = C.read_jsonl(probe)
        self.assertGreaterEqual(len(recs), STOPS * 0.9,
                                'only {} stops'.format(len(recs)))
        bad = []
        lua_stops = 0
        classes = {}
        for i, rec in enumerate(recs):
            if rec.get('ljbt') and C.entries(rec['ljbt']):
                lua_stops += 1
            k = stop_class(rec.get('ljbt'))
            classes[k] = classes.get(k, 0) + 1
            v = violations(rec)
            if v:
                bad.append(dict(rec, stop=i, violations=v))
        if bad:
            with open(wd.file('violations.jsonl'), 'w') as fh:
                for b in bad:
                    fh.write(json.dumps(b) + '\n')
        else:
            wd.cleanup()
        self.assertGreater(lua_stops, 0, 'no stop with Lua frames')
        self.assertEqual([], [(b['stop'], b['violations']) for b in bad],
                         'dumps: ' + wd.file('violations.jsonl'))
        if len(recs) >= MIN_CLASS_STOPS:
            # The checks above did not go vacuous: there are clean stops
            # where each part of lj-bt works (a mark lifts the outer frame
            # check, a stop with nothing to check checks nothing).
            for k in CLASSES[self.jit]:
                self.assertGreater(classes.get((k, 'clean'), 0), 0,
                                   'no clean stop in {}: {}'.format(
                                       k, classes))


for _jit in C.jit_modes():
    _name = 'TestStops_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (StopsMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
