# Run the vendored tests of tools/tarantool-gdb.py (tarantool/,
# unchanged) against the Tarantool extension under test:
#
#   python3 tests/dbg/upstream/run_tarantool.py [pytest arguments]
#
# - tarantool/test_gdb.py (PR #13078): tt-fiber and info tt-fibers on
#   every fiber, $tl_var_* on every thread, live and on a core;
# - tt_mp_test.py: tarantool/test-tarantool-gdb.py (PR #9665), tt-mp on
#   every MsgPack type, live.
#
# pytest runs them with --noconftest and the plugin tt_fixtures.py
# instead of tarantool/conftest.py: the same fixtures, pointed at
# $LJBT_TARANTOOL and the file under test, and a core written by gcore
# instead of one found through core_pattern.
#
# Environment: LJBT_TARANTOOL (required), DBG_TT_GDB (the extension
# under test, default: tarantool_gdb.py of this repo; the
# tools/tarantool-gdb.py of a release works too), DBG_GDB, DBG_PYTHON (a
# python3 with pytest,
# default: this one), TMPDIR.

import os
import platform
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if not os.environ.get('LJBT_TARANTOOL'):
        sys.exit('set LJBT_TARANTOOL to a tarantool binary with DWARF')
    python = os.environ.get('DBG_PYTHON') or sys.executable
    if subprocess.run([python, '-c', 'import pytest'],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        sys.exit('{} has no pytest: pip install pytest, or point '
                 'DBG_PYTHON at a python3 that has it'.format(python))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH'] = os.pathsep.join(
        [HERE] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    test_gdb = os.path.join(HERE, 'tarantool', 'test_gdb.py')
    cmd = [python, '-m', 'pytest', '-v', '-rA',
           '--noconftest', '-p', 'tt_fixtures', '-p', 'no:cacheprovider',
           '--rootdir', HERE, test_gdb, os.path.join(HERE, 'tt_mp_test.py')]
    if platform.machine() in ('aarch64', 'arm64'):
        # ut_tl_var.py takes a TLS offset for negative (x86-64, TLS
        # variant II: the variables below the thread pointer): on
        # AArch64 they are above it.  test_tarantool.py checks the
        # $tl_var_* functions there.
        print('run_tarantool.py: not run on AArch64, ut_tl_var.py wants '
              'the x86-64 TLS layout: test_tl_var_live, '
              'test_tl_var_postmortem')
        for t in ('test_tl_var_live', 'test_tl_var_postmortem'):
            # A node id is relative to the rootdir.
            cmd += ['--deselect', 'tarantool/test_gdb.py::' + t]
    cmd += sys.argv[1:]
    # The tests run gdb and tarantool in the current directory.
    with tempfile.TemporaryDirectory(prefix='dbg-tt-upstream-') as wd:
        sys.exit(subprocess.run(cmd, env=env, cwd=wd).returncode)


if __name__ == '__main__':
    main()
