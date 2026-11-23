# A pytest plugin with the fixtures of tarantool/conftest.py (the
# vendored test/gdb/conftest.py of tarantool, which run_tarantool.py
# does not load: --noconftest), pointed at this repository:
#
#   tarantool_bin       $TARANTOOL_BIN, else $LJBT_TARANTOOL
#   tarantool_gdb_ext   $DBG_TT_GDB, else the tarantool-gdb.py of this
#                       repository
#   tarantool_gdb       utils.GdbSession of the vendored utils.py, with
#                       $DBG_GDB (default gdb)
#   tarantool_coredump  the crash of the upstream fixture, written by
#                       gdb's gcore at the SIGSEGV: no core_pattern, no
#                       ulimit, no ENABLE_COREDUMP_TESTS
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(HERE, 'tarantool'))

import utils  # noqa: E402  (tarantool/utils.py)

GDB = os.environ.get('DBG_GDB', 'gdb')
# The crash of tarantool/conftest.py.
CRASH = "require('ffi').cast('char *', 0)[0] = 42"


@pytest.fixture(scope='session')
def tarantool_bin():
    path = os.environ.get('TARANTOOL_BIN') or os.environ.get('LJBT_TARANTOOL')
    if not path:
        pytest.exit('set LJBT_TARANTOOL to a tarantool binary with DWARF')
    return path


@pytest.fixture(scope='session')
def tarantool_gdb_ext():
    return os.path.abspath(os.environ.get('DBG_TT_GDB') or
                           os.path.join(ROOT, 'tarantool-gdb.py'))


@pytest.fixture(scope='session')
def tarantool_gdb(tarantool_bin, tarantool_gdb_ext):
    return utils.GdbSession(GDB, tarantool_bin, tarantool_gdb_ext)


def gcore_crash(tarantool_bin, work_dir):
    '''Run the crash under gdb, write a core at the signal.'''
    core = os.path.join(str(work_dir), 'core')
    p = subprocess.run([GDB, '-batch', '-nx',
                        '-ex', 'set startup-with-shell off',
                        '-ex', 'run',
                        '-ex', 'gcore ' + core,
                        '--args', tarantool_bin, '-e', CRASH],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, universal_newlines=True,
                       cwd=str(work_dir), timeout=120)
    assert 'SIGSEGV' in p.stdout, p.stdout[-3000:]
    assert os.path.exists(core), p.stdout[-3000:]
    return core


@pytest.fixture(scope='session')
def tarantool_coredump(tarantool_bin, tmpdir_factory):
    return gcore_crash(tarantool_bin, tmpdir_factory.mktemp('coredump'))
