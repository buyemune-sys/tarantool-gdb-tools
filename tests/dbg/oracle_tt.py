# gdb functions and commands the tests of tarantool-gdb.py compare it
# with.  They read the tarantool structures on their own, with nothing
# of tarantool-gdb.py, so that a test does not check the extension
# against itself.
import gdb


def _mem_error(f):
    def wrapper(*args):
        try:
            return f(*args)
        except gdb.MemoryError as e:
            raise gdb.GdbError(str(e))
    return wrapper


class RlistLen(gdb.Function):
    '''$dbg_rlist_len(struct rlist *head): the number of items of an
    rlist, the head not counted.'''

    def __init__(self):
        super(RlistLen, self).__init__('dbg_rlist_len')

    @_mem_error
    def invoke(self, head):
        n = 0
        item = head['next']
        while int(item) != int(head):
            n += 1
            item = item['next']
        return n


class StailqLen(gdb.Function):
    '''$dbg_stailq_len(struct stailq *q): the number of entries.'''

    def __init__(self):
        super(StailqLen, self).__init__('dbg_stailq_len')

    @staticmethod
    def ptr(e):
        # Since 3.2 (?) the links are struct stailq_entry_ptr {value}.
        if e.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT:
            return e['value']
        return e

    @_mem_error
    def invoke(self, q):
        n = 0
        e = self.ptr(q['first'])
        while int(e) != 0:
            n += 1
            e = self.ptr(e['next'])
        return n


class FiberId(gdb.Function):
    '''$dbg_fiber_id(name): the id of the fiber of main_cord with that
    name.'''

    def __init__(self):
        super(FiberId, self).__init__('dbg_fiber_id')

    @_mem_error
    def invoke(self, name):
        name = name.string()
        fiber = gdb.lookup_type('struct fiber')
        off = fiber['link'].bitpos // 8
        head = gdb.parse_and_eval('&main_cord.alive')
        item = head['next']
        while int(item) != int(head):
            f = gdb.Value(int(item) - off).cast(fiber.pointer())
            if f['name'].string() == name:
                return int(f['fid'])
            item = item['next']
        raise gdb.GdbError('no fiber "{}"'.format(name))


class Threads(gdb.Command):
    '''dbg-threads COMMAND: run COMMAND in every thread, in the order of
    their LWPs (the same live and on a core, unlike the thread numbers),
    each output under "### <rank>"; an error is printed as "!error: ...".
    The selected thread is restored.'''

    def __init__(self):
        super(Threads, self).__init__('dbg-threads', gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        orig = gdb.selected_thread()
        out = []
        try:
            threads = sorted(gdb.selected_inferior().threads(),
                             key=lambda t: t.ptid[1])
            for rank, t in enumerate(threads):
                t.switch()
                try:
                    res = gdb.execute(arg, False, True)
                except gdb.error as e:
                    res = '!error: {}'.format(e)
                out.append('### {}\n{}\n'.format(rank, res.rstrip('\n')))
        finally:
            orig.switch()
        gdb.write(''.join(out))


RlistLen()
StailqLen()
FiberId()
Threads()
