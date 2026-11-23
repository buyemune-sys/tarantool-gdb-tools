# gdb convenience functions the tests compare the extensions with.  They
# read the LuaJIT structures on their own, with nothing of
# luajit_dbg.py, so that a test does not check the extension against
# itself.
import gdb


def _ref(v, kind):
    '''The address in a GCRef (kind 'gcptr') or an MRef ('ptr').'''
    t = v.type.strip_typedefs()
    names = [f.name for f in t.fields()]
    try:
        return int(v[kind + '64'] if kind + '64' in names
                   else v[kind + '32'])
    except gdb.MemoryError as e:
        raise gdb.GdbError(str(e))


class GCListLen(gdb.Function):
    '''$dbg_gclen(GCRef head, ring): the length of a GC object list linked
    by gch.nextgc; ring: the list is a ring (g->gc.mmudata) whose head
    points to its last object.'''

    def __init__(self):
        super(GCListLen, self).__init__('dbg_gclen')

    def invoke(self, head, ring=0):
        try:
            return self.count(head, ring)
        except gdb.MemoryError as e:
            raise gdb.GdbError(str(e))

    def count(self, head, ring):
        gcobj = gdb.lookup_type('GCobj').pointer()
        first = _ref(head, 'gcptr')
        if not first:
            return 0
        if int(ring):
            # The head is the last object; its nextgc is the first one.
            last = first
            p = _ref(gdb.Value(last).cast(gcobj)['gch']['nextgc'], 'gcptr')
            n = 1
            while p != last:
                n += 1
                p = _ref(gdb.Value(p).cast(gcobj)['gch']['nextgc'], 'gcptr')
            return n
        n = 0
        p = first
        while p:
            n += 1
            p = _ref(gdb.Value(p).cast(gcobj)['gch']['nextgc'], 'gcptr')
        return n


class MRefAddr(gdb.Function):
    '''$dbg_mref(MRef): the address in an MRef, whatever LJ_GC64 is.'''

    def __init__(self):
        super(MRefAddr, self).__init__('dbg_mref')

    def invoke(self, ref):
        return _ref(ref, 'ptr')


class GCRefAddr(gdb.Function):
    '''$dbg_gcref(GCRef): the address in a GCRef.'''

    def __init__(self):
        super(GCRefAddr, self).__init__('dbg_gcref')

    def invoke(self, ref):
        return _ref(ref, 'gcptr')


class IRIsIns(gdb.Function):
    '''$dbg_ir_isins(GCtrace *T, ref): ref is the start of an IR
    instruction of T (not the second slot of a 64-bit constant), from
    T->nk up to REF_BIAS (lj_ir.c: ir_nextk64 puts the payload in the
    slot above the constant).'''

    REF_BIAS = 0x8000

    def __init__(self):
        super(IRIsIns, self).__init__('dbg_ir_isins')

    def invoke(self, T, ref):
        ref = int(ref)
        if ref >= self.REF_BIAS:
            return int(ref < int(T['nins']))
        two = ['IR_KNUM', 'IR_KINT64']
        if gdb.lookup_type('GCRef').strip_typedefs().sizeof == 8:
            two += ['IR_KGC', 'IR_KPTR', 'IR_KKPTR']
        two = set(int(gdb.parse_and_eval(n)) for n in two)
        r = int(T['nk'])
        while r < self.REF_BIAS:
            if r == ref:
                return 1
            r += 2 if int(T['ir'][r]['o']) in two else 1
        return 0


GCListLen()
MRefAddr()
GCRefAddr()
IRIsIns()
