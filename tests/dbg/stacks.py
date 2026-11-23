# Runs inside gdb, sourced by the plans of test_cores.py.
#
#   dbg-stacks [live]
#
# prints, as JSON: {"gdb": gdb.VERSION, "pid": the inferior's pid,
# "threads": [...]}, a thread per LWP in their order (the same live and
# on a core, unlike the thread numbers):
#   lwp, name   the LWP, the thread name (live; a core has none)
#   sp          $sp of its innermost frame
#   mem         the 1 KiB above $sp, hex; null and "err" if unreadable
#   frames      the functions of its backtrace (64 at most)
# and with "live", from /proc/PID/maps of the process:
#   vma         [start, end) of the mapping that holds $sp
#   unreadable  [[start, end), ...]: the pages of that mapping that
#               cannot be read (a guard page installed with
#               MADV_GUARD_INSTALL is one: EIO from /proc/PID/mem)
# The selected thread is restored.
import json

import gdb

MEM = 1024
CHUNK = 1 << 20
PAGE = 4096


def vma_of(pid, addr):
    with open('/proc/{}/maps'.format(pid)) as fh:
        for line in fh:
            lo, hi = (int(x, 16) for x in line.split()[0].split('-'))
            if lo <= addr < hi:
                return [lo, hi]
    return None


def unreadable(inf, lo, hi):
    runs = []
    for c in range(lo, hi, CHUNK):
        n = min(CHUNK, hi - c)
        try:
            inf.read_memory(c, n)
            continue
        except gdb.MemoryError:
            pass
        for p in range(c, c + n, PAGE):
            try:
                inf.read_memory(p, PAGE)
            except gdb.MemoryError:
                if runs and runs[-1][1] == p:
                    runs[-1][1] = p + PAGE
                else:
                    runs.append([p, p + PAGE])
    return runs


def frames():
    names = []
    f = gdb.newest_frame()
    while f is not None and len(names) < 64:
        names.append(f.name() or '??')
        try:
            f = f.older()
        except gdb.error:
            break
    return names


class Stacks(gdb.Command):
    '''dbg-stacks [live]: the stack of every thread as JSON.'''

    def __init__(self):
        super(Stacks, self).__init__('dbg-stacks', gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        live = arg.strip() == 'live'
        inf = gdb.selected_inferior()
        orig = gdb.selected_thread()
        out = []
        scanned = {}
        try:
            for t in sorted(inf.threads(), key=lambda t: t.ptid[1]):
                t.switch()
                sp = int(gdb.parse_and_eval('$sp'))
                rec = {'lwp': t.ptid[1], 'name': t.name, 'sp': sp,
                       'frames': frames()}
                try:
                    rec['mem'] = bytes(inf.read_memory(sp, MEM)).hex()
                except gdb.MemoryError as e:
                    rec['mem'], rec['err'] = None, str(e)
                if live:
                    rec['vma'] = vma_of(inf.pid, sp)
                    if rec['vma']:
                        key = tuple(rec['vma'])
                        if key not in scanned:
                            scanned[key] = unreadable(inf, *key)
                        rec['unreadable'] = scanned[key]
                out.append(rec)
        finally:
            orig.switch()
        gdb.write(json.dumps({'gdb': gdb.VERSION, 'pid': inf.pid,
                              'threads': out}) + '\n')


Stacks()
