# The commands of a Scenario on a core after another core.
#
# What an extension keeps from one program it looked at may be wrong
# for the next one: a gdb.Value holds the address its variable had when
# it was made, and a PIE (a dynamic build of tarantool) is at another
# address in each process (ASLR); a cache may hold addresses of the
# other process.  OtherCore runs a scenario twice with ASLR on, then one
# gdb session on the core of the first run that runs the commands,
# switches to the core of the second (`core-file`) and runs them again:
# every output there must be the one of a fresh session on that core
# (the core mode of the second run).
#
# The official static builds are not PIEs: both processes have the
# binary at its link-time address, only the heap and the stacks move.

import json
import os
import re

import harness as H

# Before `run`, with the breaks: gdb turns ASLR off by default.
ASLR = 'set disable-randomization off'
HISTORY = re.compile(r'^\$\d+ = ', re.M)
# Where the binary is in each process.
WHERE = 'eval:(long)&main'


def _norm(r):
    '''(out, err) of a command result, without what two sessions print
    differently: the value history numbers, the warnings of gcore cores.'''
    if r is None or r.get('skipped'):
        return None
    out = r['out']
    if isinstance(out, str):
        out = HISTORY.sub('', H.strip_core_warnings(out))
    return out, r['err']


class OtherCore(object):
    '''scen, run twice, its commands on the second core after the first
    in one session.  scenarios: the two runs, for SCENARIOS (their work
    directories are removed with them).'''

    def __init__(self, scen):
        kw = dict(breaks=[ASLR] + list(scen.breaks), keep_core=True,
                  live_only=None)
        self.name = scen.name
        self.first = scen.variant(scen.name + '-a', **kw)
        self.second = scen.variant(scen.name + '-b', **kw)
        self.scenarios = (self.first, self.second)
        self._res = None

    def run(self):
        if self._res is not None:
            return self._res
        runs = [s.run() for s in self.scenarios]
        cores = [os.path.join(r['wd'], 'core') for r in runs]
        plans = []
        for r in runs:
            with open(os.path.join(r['wd'], 'core.plan.json')) as fh:
                plans.append(json.load(fh))
        first, second = plans
        res = {'fatal': None, 'cmds': {}}
        for core in cores:
            if not os.path.exists(core):
                res['fatal'] = 'no core ' + core
        if res['fatal'] is None:
            cmds = [['@where.a', WHERE]] + first['cmds'] + \
                [['@switch', 'core-file ' + cores[1]], ['@where.b', WHERE]] + \
                [['@prep.{}'.format(i), c]
                 for i, c in enumerate(second['prep'])] + \
                [['@b:' + c[0]] + list(c[1:]) for c in second['cmds']]
            res = self.first._gdb(runs[0]['wd'], dict(first, cmds=cmds),
                                  'othercore')
        for core in cores:
            if H.KEEP != 'cores' and os.path.exists(core):
                os.unlink(core)
        self._res = {'after': res, 'fresh': runs[1]['core']}
        return self._res

    def moved(self):
        '''The binary is at another address in the second process.'''
        cmds = self.run()['after']['cmds']
        where = [cmds.get(k, {}).get('out') for k in ('@where.a', '@where.b')]
        return None not in where and where[0] != where[1]

    def diffs(self):
        '''[(key, on the second core after the first, fresh)] of the
        commands whose outputs differ, or a fatal error of either.'''
        res = self.run()
        after, fresh = res['after'], res['fresh']
        for r in (after, fresh):
            if r.get('fatal'):
                return [('fatal', r['fatal'], None)]
        out = []
        for key in sorted(fresh['cmds']):
            a = _norm(after['cmds'].get('@b:' + key))
            f = _norm(fresh['cmds'][key])
            if a != f:
                out.append((key, a, f))
        return out
