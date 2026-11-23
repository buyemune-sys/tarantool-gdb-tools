# Run the vendored LuaJIT debugger tests (luajit/debug-extension-tests.py,
# unchanged) against tarantool instead of the luajit binary:
#
#   python3 tests/dbg/upstream/run_luajit.py [unittest arguments]
#
# Environment: LJBT_TARANTOOL (required), DBG_LUAJIT_DBG (the extension
# under test, default: luajit_gdb.py of this repo; the luajit_dbg.py or
# the old luajit-gdb.py of a release works too), DBG_UPSTREAM_TIMEOUT
# (seconds per gdb run, default 60; the
# upstream 10 is short for lj-gc on a loaded machine), DBG_DEBUGGER
# (gdb, the default, or lldb: the upstream file runs either), DBG_GDB,
# DBG_LLDB, TMPDIR.  The upstream file reads its settings from the
# environment; this sets them:
#
#   LUAJIT_TEST_BINARY       the tarantool binary
#   DEBUGGER_EXTENSION_PATH  a directory with luajit_dbg.py (the name
#                            the upstream file wants), a link to the
#                            extension under test
#   DEBUGGER_COMMAND         gdb-tarantool, gdb with tarantool.gdb, or
#                            lldb-tarantool (by name, this directory
#                            put in PATH)
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
# CI runs 45128549373 (3.2, 3.3, 3.5, 3.6) and 45141719368 (3.0, 3.1,
# 3.4): at the stop after `n` in lj_cf_dofile, gdb says "value has been
# optimized out" for fname in every arm64 build before 3.7; 3.7+ keep it.
OPTIMIZED_OUT = ('the arm64 build of {} has no location for fname of '
                 'lj_cf_dofile at the stop of the test: "value has been '
                 'optimized out"')
NO_FNAME_ARM64 = [(3, 0), (3, 1), (3, 2), (3, 3), (3, 4), (3, 5), (3, 6)]
# lldb places a breakpoint by name otherwise than gdb (3.9.0 x86_64,
# lldb 18 and 22).
LLDB_COLD = ('lldb puts the breakpoint of gc_sweep_str_chain, inlined, at '
             'the start of its range in the cold part of gc_onestep (gdb: '
             'lj_gc.c:425 in the hot part), which the test never reaches')
LLDB_ENTRY = ('lldb stops at the entry of lj_cf_dofile, gdb after its '
              'prologue: after the one `n` of the test fname is not set '
              'yet, "variable not available"')

# The upstream file takes LJ_DUALNUM from ffi.abi('dualnum'), an option
# the LuaJIT of 3.0, 3.1 and 3.4 does not know (lib_ffi.c of their
# submodule commits), so it says false; their arm64 builds are
# dual-number (CI run 45141719368: lj-arch says LJ_DUALNUM: True, lj-func
# "; integer 42").  The value of harness.dual_number_mode(), read from a
# TValue, replaces that expression before the file runs.
UPSTREAM_DUALNUM = ('IS_DUALNUM = execute_process([\n'
                    '    LUAJIT_BINARY, \'-e\', '
                    '"print(require(\'ffi\').abi(\'dualnum\'))"\n'
                    ']).strip() == \'true\'\n')


def tarantool_skips(version, arch, debugger='gdb'):
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
    if arch == 'arm64' and version[:2] in NO_FNAME_ARM64:
        skips['TestLJStr'] = OPTIMIZED_OUT.format(
            '.'.join(map(str, version)))
    if debugger == 'lldb':
        skips['TestGCStateSweepString'] = LLDB_COLD
        skips['TestLJStr'] = LLDB_ENTRY
    return skips


def red_zone_any_value(mod):
    '''The lj-stack tests want every red zone slot nil.  LuaJIT does not
    keep it so: the red zone (LJ_STACK_EXTRA, lj_state.h: "Extra stack
    space (metamethods)") takes the frames and arguments of metamethod
    calls above the top, and only the atomic GC phase (lj_gc.c,
    gc_traverse_thread) and a stack growth (lj_state.c, resizestack)
    reset slots to nil.  CI run 45128549373, 3.2.4 arm64: "fast function
    pairs", true and "enterprise_edition" there.  Any value is let pass;
    test_luajit.py checks every slot against the memory.'''
    nil = r' VALUE: nil\n?)*'
    for name in ('TestLJStackBase', 'TestLJStackFunc'):
        cls = getattr(mod, name)
        if nil not in cls.pattern:
            raise RuntimeError('{}: the red zone pattern changed, review '
                               'red_zone_any_value()'.format(name))
        cls.pattern = cls.pattern.replace(nil, r' VALUE: .+\n?)*', 1)


def dual_number_mode():
    '''LJ_DUALNUM of LJBT_TARANTOOL, as tests/dbg/harness.py reads it.'''
    sys.path.insert(0, os.path.dirname(HERE))
    import harness
    return harness.IS_DUALNUM


def exec_upstream(mod, is_dualnum):
    '''Run the upstream file in mod with IS_DUALNUM = is_dualnum, so
    that RX_INT, RX_ISDUALNUM and the IR numbers derived from it follow
    the build (see UPSTREAM_DUALNUM).'''
    with open(UPSTREAM) as fh:
        src = fh.read()
    if src.count(UPSTREAM_DUALNUM) != 1:
        raise RuntimeError('the IS_DUALNUM line of the upstream file '
                           'changed, review UPSTREAM_DUALNUM')
    src = src.replace(UPSTREAM_DUALNUM, 'IS_DUALNUM = {!r}\n'.format(
        bool(is_dualnum)))
    mod.__file__ = UPSTREAM
    exec(compile(src, UPSTREAM, 'exec'), mod.__dict__)


def tarantool_info(tarantool):
    '''((major, minor, patch), jit.arch) of the binary.'''
    out = subprocess.run([tarantool, '-e',
                          'print(_TARANTOOL, jit.arch) os.exit(0)'],
                         stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         universal_newlines=True, timeout=60).stdout.split()
    return tuple(int(x) for x in out[0].split('-')[0].split('.')), out[1]


def main():
    tarantool = os.environ.get('LJBT_TARANTOOL')
    if not tarantool:
        sys.exit('set LJBT_TARANTOOL to a tarantool binary with DWARF')
    ext = os.path.abspath(os.environ.get('DBG_LUAJIT_DBG') or
                          os.path.join(ROOT, 'luajit_gdb.py'))
    timeout = int(os.environ.get('DBG_UPSTREAM_TIMEOUT', 60))
    debugger = os.environ.get('DBG_DEBUGGER', 'gdb')
    if debugger not in ('gdb', 'lldb'):
        sys.exit('DBG_DEBUGGER is gdb or lldb, not ' + debugger)
    extdir = tempfile.mkdtemp(prefix='dbg-upstream-')
    os.symlink(ext, os.path.join(extdir, 'luajit_dbg.py'))
    os.environ.update({
        'LUAJIT_TEST_BINARY': tarantool,
        'DEBUGGER_EXTENSION_PATH': extdir,
        # By name, found in PATH: the upstream file takes the debugger
        # for lldb if "lldb" is anywhere in DEBUGGER_COMMAND, and the
        # path of a checkout may have it.
        'DEBUGGER_COMMAND': debugger + '-tarantool',
        'PATH': HERE + os.pathsep + os.environ.get('PATH', ''),
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    try:
        spec = importlib.util.spec_from_file_location('debug_extension_tests',
                                                      UPSTREAM)
        mod = importlib.util.module_from_spec(spec)
        exec_upstream(mod, dual_number_mode())
        run = mod.execute_process
        mod.execute_process = lambda cmd, timeout=timeout: run(cmd, timeout)
        with open(ext) as fh:
            if "'lj-gctop'" in fh.read():
                # Not upstream yet: one more command initialized.
                gco = r'lj-gco command initialized\n'
                mod.TestLoad.pattern = mod.TestLoad.pattern.replace(
                    gco, gco + r'lj-gctop command initialized\n')
        red_zone_any_value(mod)
        for name, why in tarantool_skips(*tarantool_info(tarantool),
                                         debugger=debugger).items():
            setattr(mod, name, unittest.skip(why)(getattr(mod, name)))
        prog = unittest.main(module=mod, argv=[sys.argv[0]] + sys.argv[1:],
                             exit=False, verbosity=2)
        rc = 0 if prog.result.wasSuccessful() else 1
    finally:
        shutil.rmtree(extdir, ignore_errors=True)
    sys.exit(rc)


if __name__ == '__main__':
    main()
