# Shared helpers of the tests of luajit_dbg.py and tarantool-gdb.py.
#
# Environment:
#   LJBT_TARANTOOL   tarantool binary with DWARF (required; the name is
#                    the one tests/fetch.sh prints)
#   DBG_LUAJIT_DBG   the luajit_dbg.py under test (default: the one of
#                    this repo; point it at an upstream copy to see a
#                    test of a fix fail before the fix)
#   DBG_TT_GDB       the tarantool-gdb.py under test (default: this repo)
#   DBG_KEEP         keep the work directories (plans, results, gdb logs);
#                    DBG_KEEP=cores keeps the core files (~40 MB each) too
#   DBG_VERBOSE      print plans and results
#   TMPDIR           where work directories go
#
# A Scenario is a Lua script, a stop in it and the gdb commands to run
# there.  It runs once per process: live (gdb runs tarantool to the
# stop), then post-mortem on the gcore file written at that stop.  The
# tests read the output of each command in both modes.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DRIVER = os.path.join(HERE, 'driver.py')
ORACLE = os.path.join(HERE, 'oracle.py')
HIDE_ENUMS = os.path.join(HERE, 'hide_enums.py')
ENUMCHECK = os.path.join(HERE, 'enumcheck.py')
LUA = os.path.join(HERE, 'lua')

TARANTOOL = os.environ.get('LJBT_TARANTOOL')
LUAJIT_DBG = os.environ.get('DBG_LUAJIT_DBG') or \
    os.path.join(ROOT, 'luajit_dbg.py')
TT_GDB = os.environ.get('DBG_TT_GDB') or \
    os.path.join(ROOT, 'tarantool-gdb.py')
KEEP = os.environ.get('DBG_KEEP', '')
VERBOSE = bool(os.environ.get('DBG_VERBOSE'))
GDB = os.environ.get('DBG_GDB', 'gdb')

if not TARANTOOL:
    raise RuntimeError('set LJBT_TARANTOOL to a tarantool binary with DWARF')

PY_ERROR = 'Error occurred in Python'


def lua_eval(code, timeout=60):
    '''Run a Lua snippet with the tarantool binary, return its stdout.'''
    p = subprocess.run([TARANTOOL, '-e', code + '; os.exit(0)'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, universal_newlines=True,
                       timeout=timeout)
    return p.stdout.strip()


def tvalue_bits(lua_value, timeout=60):
    '''The 64 bits of the TValue of a Lua value, the argument of print()
    at the stop in lj_cf_print, as gdb reads them from memory.'''
    p = subprocess.run([GDB, '-batch', '-nx', '-ex', 'set startup-with-shell off',
                        '-ex', 'break lj_cf_print',
                        '-ex', 'run', '-ex', 'output/x *(unsigned long long *)L->base',
                        '-ex', 'echo \\n', '-ex', 'kill',
                        '--args', TARANTOOL, '-e',
                        'print({}) os.exit(0)'.format(lua_value)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, universal_newlines=True,
                       timeout=timeout)
    lines = [s for s in p.stdout.split('\n') if s.startswith('0x')]
    if not lines:
        raise RuntimeError('no TValue of {}: {}'.format(lua_value,
                                                        p.stdout[-2000:]))
    return int(lines[-1], 16)


def dual_number_mode():
    '''LJ_DUALNUM of the build: the integer 42 is a TValue with the int
    in its low word (setintV(), lj_obj.h), else the double 42.0.
    ffi.abi('dualnum') is no source: the LuaJIT of 3.0, 3.1 and 3.4 has no
    such option and says false for it.'''
    bits = tvalue_bits(42)
    if bits == 0x4045000000000000:          # 42.0
        return False
    if bits & 0xffffffff == 42:
        return True
    raise RuntimeError('the TValue of 42 is neither: {:#x}'.format(bits))


_abi = lua_eval("local ffi = require('ffi') "
                "print(ffi.abi('gc64'), jit.arch, _TARANTOOL)").split()
IS_GC64 = _abi[0] == 'true'
IS_DUALNUM = dual_number_mode()
ARCH = _abi[1]                  # x64 or arm64
VERSION = _abi[2]               # 3.9.0-0-g...
VERSION_TUPLE = tuple(int(x) for x in VERSION.split('-')[0].split('.'))


def mref(expr, tp):
    '''mref() of lj_obj.h as a gdb expression: no macros in the DWARF.'''
    return '(({} *)({}).ptr{})'.format(tp, expr, 64 if IS_GC64 else 32)


def gcref(expr):
    return '((GCobj *)({}).gcptr{})'.format(expr, 64 if IS_GC64 else 32)


def gcval(tv):
    '''gcval() of a TValue * expression.'''
    if IS_GC64:
        return '((GCobj *)(({})->gcr.gcptr64 & 0x7fffffffffff))'.format(tv)
    return '((GCobj *)({})->gcr.gcptr32)'.format(tv)


def G(L='L'):
    return mref('({})->glref'.format(L), 'global_State')


# GG_State of the lua_State L: L->glref points to GG->g.
def GG(L='L'):
    return '((GG_State *)((char *){} - (char *)&((GG_State *)0)->g))' \
        .format(G(L))


class Scenario(object):
    '''A script, a stop, the commands.  run() runs it once (cached):
    live, then on the core written at the stop unless live_only.'''

    def __init__(self, name, script, breaks, cmds, setup=(), prep=(),
                 sources=None, deferred=False, live_only=None,
                 timeout=120, env=None, gdb_env=None):
        self.name = name
        self.script = script
        self.breaks = list(breaks)
        self.cmds = list(cmds.items()) if isinstance(cmds, dict) \
            else list(cmds)
        self.setup = list(setup)
        self.prep = list(prep)
        self.sources = sources
        self.deferred = deferred
        self.live_only = live_only      # the reason, if any
        self.timeout = timeout
        self.env = env or {}            # of the inferior
        self.gdb_env = gdb_env or {}    # of gdb
        self._res = None

    def variant(self, name, **kw):
        '''The same scenario with other settings (sources, gdb_env...).'''
        args = dict(script=self.script, breaks=self.breaks, cmds=self.cmds,
                    setup=self.setup, prep=self.prep, sources=self.sources,
                    deferred=self.deferred, live_only=self.live_only,
                    timeout=self.timeout, env=self.env, gdb_env=self.gdb_env)
        args.update(kw)
        return Scenario(name, **args)

    def _gdb(self, wd, plan, tag):
        plan = dict(plan, out=os.path.join(wd, tag + '.json'))
        pfile = os.path.join(wd, tag + '.plan.json')
        with open(pfile, 'w') as fh:
            json.dump(plan, fh, indent=1)
        env = dict(os.environ, DBG_PLAN=pfile, **self.gdb_env)
        p = subprocess.run([GDB, '-batch', '-nx', '-x', DRIVER],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, universal_newlines=True,
                           timeout=self.timeout, env=env, cwd=wd)
        with open(os.path.join(wd, tag + '.gdb.log'), 'w') as fh:
            fh.write(p.stdout)
        try:
            with open(plan['out']) as fh:
                res = json.load(fh)
        except (IOError, ValueError):
            res = {'fatal': 'no result, gdb said:\n' + p.stdout[-3000:],
                   'cmds': {}, 'load': '', 'run': '', 'setup': []}
        res['log'] = p.stdout
        if VERBOSE:
            sys.stderr.write('\n### {} {}\n{}\n'.format(
                self.name, tag, json.dumps(res, indent=1)))
        return res

    def run(self):
        if self._res is not None:
            return self._res
        wd = tempfile.mkdtemp(prefix='dbg-{}-'.format(self.name))
        script = os.path.join(wd, 'script.lua')
        with open(script, 'w') as fh:
            fh.write(self.text())
        core = os.path.join(wd, 'core')
        sources = self.sources if self.sources is not None \
            else [LUAJIT_DBG, ORACLE]

        def subst(cmds):
            # @SCRIPT@: the path of the script, as chunk names have it.
            return [[x.replace('@SCRIPT@', script) for x in c]
                    if isinstance(c, (list, tuple))
                    else c.replace('@SCRIPT@', script) for c in cmds]
        plan = {
            'binary': TARANTOOL, 'args': [script], 'cwd': wd,
            'env': self.env, 'sources': sources, 'deferred': self.deferred,
            'breaks': subst(self.breaks), 'setup': subst(self.setup),
            'prep': subst(self.prep), 'cmds': subst(self.cmds),
            'gcore': None if self.live_only else core,
        }
        res = {'live': self._gdb(wd, plan, 'live'), 'wd': wd}
        if not self.live_only:
            if os.path.exists(core):
                cplan = dict(plan, core=core, gcore=None)
                res['core'] = self._gdb(wd, cplan, 'core')
            else:
                res['core'] = {'fatal': 'no core file was written',
                               'cmds': {}, 'load': '', 'setup': []}
        self._res = res
        if KEEP != 'cores' and os.path.exists(core):
            os.unlink(core)
        return res

    def text(self):
        '''The script as tarantool runs it: the prelude, then the
        scenario's own lines.'''
        with open(os.path.join(LUA, 'prelude.lua')) as fh:
            return fh.read() + self.script

    def line(self, marker):
        '''The line number of the script line that holds marker.'''
        for n, s in enumerate(self.text().split('\n'), 1):
            if marker in s:
                return n
        raise KeyError(marker)

    def path(self, name):
        return os.path.join(self.run()['wd'], name)

    def script_path(self):
        return self.path('script.lua')

    def read(self, name):
        '''A file the script wrote in its working directory.'''
        with open(self.path(name)) as fh:
            return fh.read()

    def oracle(self):
        if 'oracle' not in self.run():
            self._res['oracle'] = json.loads(self.read('oracle.json'))
        return self._res['oracle']

    def modes(self):
        return ['live'] if self.live_only else ['live', 'core']

    def cleanup(self):
        if self._res is not None and not KEEP:
            shutil.rmtree(self._res['wd'], ignore_errors=True)


class DbgCase(unittest.TestCase):
    '''Tests over Scenario outputs.  SCENARIOS of a class are removed in
    tearDownClass.'''

    SCENARIOS = ()
    maxDiff = None

    @classmethod
    def tearDownClass(cls):
        for s in cls.SCENARIOS:
            s.cleanup()

    def result(self, scen, mode='live'):
        res = scen.run()[mode]
        self.assertIsNone(res.get('fatal'), res.get('fatal'))
        return res

    def raw(self, scen, key, mode='live'):
        res = self.result(scen, mode)
        self.assertIn(key, res['cmds'], 'no command {}'.format(key))
        return res['cmds'][key]

    def value(self, scen, key, mode='live'):
        '''The value of an "eval:" command.'''
        r = self.raw(scen, key, mode)
        self.assertIsNone(r['err'], '{}: {}'.format(key, r['err']))
        return r['out']

    def outs(self, scen, key):
        '''[(mode, output)] of a command that must succeed, live and on
        the core; the core output must be the live one.'''
        got = []
        for mode in scen.modes():
            r = self.raw(scen, key, mode)
            self.assertIsNone(r['err'], '{} ({}): {}\n{}'.format(
                key, mode, r['err'], self.result(scen, mode)['log'][-2000:]))
            got.append((mode, r['out']))
        if len(got) == 2:
            self.assertEqual(got[0][1], got[1][1],
                             '{}: the core output differs'.format(key))
        return got

    def out(self, scen, key):
        '''The output of a command (checked in both modes, see outs).'''
        return self.outs(scen, key)[0][1]

    def errs(self, scen, key):
        '''[(mode, error message)] of a command that must fail.'''
        got = []
        for mode in scen.modes():
            r = self.raw(scen, key, mode)
            self.assertIsNotNone(r['err'], '{} ({}) did not fail: {}'
                                 .format(key, mode, r['out']))
            got.append((mode, r['err']))
        return got

    def assertNoPyError(self, scen):
        for mode in scen.modes():
            res = self.result(scen, mode)
            for key, r in res['cmds'].items():
                self.assertNotIn(PY_ERROR, r['err'] or '',
                                 '{} ({})'.format(key, mode))
            self.assertNotIn('Traceback', res['log'],
                             '{}: {}'.format(mode, res['log'][-3000:]))
