# The MsgPack tests of PR #9665 (tarantool/test-tarantool-gdb.py,
# unchanged) as a pytest test, run the way test_gdb.py runs its
# ut_*.py: gdb, a stop at run_script_f, source the extension and the
# file, unittest.main() for the exit code.  The file also runs its tests
# itself when sourced: they run twice, the second run decides.  Live
# only: tt-mp prints a string literal, which gdb has to put into the
# memory of a process.
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def test_msgpack_live(tarantool_gdb):
    tarantool_gdb.test_live(
        os.path.join(HERE, 'tarantool', 'test-tarantool-gdb.py'),
        'run_script_f', '-e', "print('Hi')")
