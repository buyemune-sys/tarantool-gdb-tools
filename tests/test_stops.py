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
    if C.warnings(doc):
        return probs            # Marked: whatever it shows is flagged.
    if not outer_found(doc):
        probs.append('outer Lua frame not found, no "!!"')
    probs += [p for p in C.check_doc(rec, allow_warnings=True)
              if not p.startswith('C frames')]
    return probs


class StopsMixin(object):
    jit = 'on'

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
        for i, rec in enumerate(recs):
            if rec.get('ljbt') and C.entries(rec['ljbt']):
                lua_stops += 1
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


for _jit in C.jit_modes():
    _name = 'TestStops_jit_{}'.format(_jit)
    globals()[_name] = type(_name, (StopsMixin, unittest.TestCase),
                            {'jit': _jit})

if __name__ == '__main__':
    unittest.main()
