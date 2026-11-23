# Run the vendored LuaJIT debugger tests (luajit/debug-extension-tests.py,
# unchanged) against tarantool instead of the luajit binary:
#
#   python3 tests/dbg/upstream/run_luajit.py [unittest arguments]
#
# Environment: LJBT_TARANTOOL (required), DBG_LUAJIT_DBG (the extension
# under test, default: luajit_dbg.py of this repo; an old luajit-gdb.py
# works too), DBG_UPSTREAM_TIMEOUT (seconds per gdb run, default 60; the
# upstream 10 is short for lj-gc on a loaded machine), DBG_GDB, TMPDIR.
# The upstream file reads its settings from the environment; this sets
# them:
#
#   LUAJIT_TEST_BINARY       the tarantool binary
#   DEBUGGER_EXTENSION_PATH  a directory with luajit_dbg.py, a link to
#                            the extension under test
#   DEBUGGER_COMMAND         gdb-tarantool, gdb with tarantool.gdb
#
# Tests that cannot pass against a tarantool binary whatever the
# extension does are skipped with the reason (tarantool_skips()); the
# rest must pass.

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
UPSTREAM = os.path.join(HERE, 'luajit', 'debug-extension-tests.py')

PRINT = ('print of tarantool is a Lua wrapper (builtin/internal.print.lua) '
         'and require a Lua function, not fast/C functions')
STARTUP = 'the first stop at {} is in the Lua of tarantool\'s own startup'
GETPID = 'tarantool declares getpid() itself, the cdef of the test clashes'
HOOK = ('every trace of tarantool starts with its own XLOAD/BAND/EQ check, '
        'the IR is not the one of plain LuaJIT')
EARLY_GC = ('before 3.7 the first GC cycle runs at startup before '
            'tarantool_L is set: no Lua state to show')


def tarantool_skips(version):
    '''{class name: why it cannot pass against this tarantool}.
    tests/dbg/test_luajit.py has these checks in the form tarantool
    allows.'''
    skips = {
        'TestLJTV': PRINT,
        'TestLJGCo': PRINT,
        'TestLJFuncFFKnown': PRINT,
        'TestLJCTypeBase': STARTUP.format('lj_cf_ffi_new'),
        'TestLJTab': STARTUP.format('lj_cf_unpack'),
        'TestLJIRConst': STARTUP.format('trace_stop') + '; ' + HOOK,
        'TestLJCTypeFunc': GETPID,
        'TestLJTraceIRRange': GETPID,
        'TestLJTraceBase': HOOK,
    }
    if version < (3, 7):
        for c in ('Pause', 'Propagate', 'Atomic', 'SweepString', 'Sweep',
                  'Finalize'):
            skips['TestGCState' + c] = EARLY_GC
    return skips


def tarantool_version(tarantool):
    out = subprocess.run([tarantool, '-e', 'print(_TARANTOOL) os.exit(0)'],
                         stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         universal_newlines=True, timeout=60).stdout
    return tuple(int(x) for x in out.strip().split('-')[0].split('.'))


def main():
    tarantool = os.environ.get('LJBT_TARANTOOL')
    if not tarantool:
        sys.exit('set LJBT_TARANTOOL to a tarantool binary with DWARF')
    ext = os.path.abspath(os.environ.get('DBG_LUAJIT_DBG') or
                          os.path.join(ROOT, 'luajit_dbg.py'))
    timeout = int(os.environ.get('DBG_UPSTREAM_TIMEOUT', 60))
    extdir = tempfile.mkdtemp(prefix='dbg-upstream-')
    os.symlink(ext, os.path.join(extdir, 'luajit_dbg.py'))
    os.environ.update({
        'LUAJIT_TEST_BINARY': tarantool,
        'DEBUGGER_EXTENSION_PATH': extdir,
        'DEBUGGER_COMMAND': os.path.join(HERE, 'gdb-tarantool'),
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    try:
        spec = importlib.util.spec_from_file_location('debug_extension_tests',
                                                      UPSTREAM)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        run = mod.execute_process
        mod.execute_process = lambda cmd, timeout=timeout: run(cmd, timeout)
        with open(ext) as fh:
            if "'lj-gctop'" in fh.read():
                # Not upstream yet: one more command initialized.
                gco = r'lj-gco command initialized\n'
                mod.TestLoad.pattern = mod.TestLoad.pattern.replace(
                    gco, gco + r'lj-gctop command initialized\n')
        for name, why in tarantool_skips(
                tarantool_version(tarantool)).items():
            setattr(mod, name, unittest.skip(why)(getattr(mod, name)))
        prog = unittest.main(module=mod, argv=[sys.argv[0]] + sys.argv[1:],
                             exit=False, verbosity=2)
        rc = 0 if prog.result.wasSuccessful() else 1
    finally:
        shutil.rmtree(extdir, ignore_errors=True)
    sys.exit(rc)


if __name__ == '__main__':
    main()
