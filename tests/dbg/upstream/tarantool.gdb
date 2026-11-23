# gdb init file of the upstream tests run against tarantool (see
# gdb-tarantool): before every `run`, the inferior arguments get the
# options of $DBG_UPSTREAM_ARGS in front, by default `-e jit.flush()`.
#
# The LuaJIT tests take `J->trace[1]` for the first trace of their
# script; tarantool has compiled traces of its own by then, the flush
# frees their numbers.
python
import os
import gdb


def dbg_upstream_args():
    pre = os.environ.get('DBG_UPSTREAM_ARGS', "-e 'jit.flush()'")
    args = gdb.parameter('args') or ''
    if pre and not args.startswith(pre):
        gdb.execute('set args {} {}'.format(pre, args))
end

define hook-run
python dbg_upstream_args()
end
