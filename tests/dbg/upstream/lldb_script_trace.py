# Imported into lldb by lldb-tarantool: the callback of the breakpoints
# of the upstream tests at trace_stop, what $dbg_script_trace() of
# tarantool.gdb is in gdb.  It stops only in a trace that started in the
# script of the test, the last inferior argument (or where it cannot
# tell); a trace of tarantool may stop before it.
import lldb


def gcref(target, ref, tp):
    '''The object of a GCRef (an SBValue) as an SBValue of type tp.'''
    ptr = ref.GetChildMemberWithName('gcptr64')
    if not ptr.IsValid():
        ptr = ref.GetChildMemberWithName('gcptr32')
    return target.CreateValueFromAddress(
        tp, lldb.SBAddress(ptr.GetValueAsUnsigned(), target),
        target.FindFirstType(tp))


def trace_chunk(frame):
    '''The chunk name of the start of J->cur, J of the frame.'''
    target = frame.GetThread().GetProcess().GetTarget()
    cur = frame.FindVariable('J').Dereference().GetChildMemberWithName('cur')
    pt = gcref(target, cur.GetChildMemberWithName('startpt'), 'GCproto')
    name = gcref(target, pt.GetChildMemberWithName('chunkname'), 'GCstr')
    err = lldb.SBError()
    s = target.GetProcess().ReadCStringFromMemory(
        name.GetLoadAddress() + name.GetByteSize(), 4096, err)
    if err.Fail():
        raise ValueError(err.GetCString())
    return s


def script_trace(frame, bp_loc, extra_args, internal_dict):
    info = frame.GetThread().GetProcess().GetTarget().GetLaunchInfo()
    n = info.GetNumArguments()
    if n == 0:
        return True
    try:
        chunk = trace_chunk(frame)
    except (ValueError, AttributeError):
        return True
    return chunk == '@' + info.GetArgumentAtIndex(n - 1)
