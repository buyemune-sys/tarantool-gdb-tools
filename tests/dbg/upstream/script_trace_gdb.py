# Sourced into gdb by gdb-tarantool, after tarantool.gdb: the
# breakpoints of the upstream tests at trace_stop stop only in a trace
# of the script of the test (lldb_script_trace.py does it in lldb).
#
# The first trace that stops may be one of tarantool: a trace it began
# before the script ran, stitched at the call of jit.opt.start (CI run
# 45154576723, 3.9.0 arm64: TestLJJSlotsBase got the slots of a caller
# of debug.getinfo, all constants).  The hook-run of tarantool.gdb calls
# dbg_script_traces(): every breakpoint at trace_stop gets the condition
# $dbg_script_trace(), the trace J->cur started in the script, the last
# inferior argument.
import shlex

import gdb


def dbg_gcref(ref, tp):
    '''The object of a GCRef, as a pointer to tp.'''
    names = [f.name for f in ref.type.strip_typedefs().fields()]
    ptr = ref['gcptr64' if 'gcptr64' in names else 'gcptr32']
    return ptr.cast(gdb.lookup_type(tp).pointer())


def dbg_trace_chunk():
    '''The chunk name of the start of the trace being recorded, J->cur
    (J of the selected frame, trace_stop).'''
    pt = dbg_gcref(gdb.parse_and_eval('J')['cur']['startpt'], 'GCproto')
    name = dbg_gcref(pt['chunkname'], 'GCstr')
    return (name + 1).cast(gdb.lookup_type('char').pointer()).string()


class DbgScriptTrace(gdb.Function):
    '''$dbg_script_trace(): 1 if the trace trace_stop stops started in
    the script of the test, 0 if in other code, 1 if it cannot tell.'''

    def __init__(self):
        super(DbgScriptTrace, self).__init__('dbg_script_trace')

    def invoke(self):
        args = shlex.split(gdb.parameter('args') or '')
        try:
            chunk = dbg_trace_chunk()
        except (gdb.error, IndexError, ValueError):
            return 1
        return int(not args or chunk == '@' + args[-1])


DbgScriptTrace()


def dbg_script_traces():
    for bp in gdb.breakpoints():
        if bp.location == 'trace_stop' and bp.condition is None:
            bp.condition = '$dbg_script_trace()'
