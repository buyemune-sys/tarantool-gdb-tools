# gdb side of the lj-bt tests: sourced after luajit_dbg.py and lj_bt.py.
#
#   ljbt-probe FILE [--filter] [--core]
#
# appends one JSON line to FILE: the `lj-bt --json` document, the frames
# of `bt -no-filters` (and without the trace unwinder, if a trace frame
# is there), the number of lj-stack frames of every lua_State lj-bt saw,
# the stop's sequence number (from the breakpoint's arguments) and any
# Python exception of lj-bt.

import json
import re
import traceback

import gdb

import __main__ as M

RX_BT = re.compile(r'^#(\d+)\s+(?:0x[0-9a-f]+ in )?(.*?)(?: \(.*)?$')


def probe_parse_bt(text):
    res = []
    for line in text.splitlines():
        m = RX_BT.match(line)
        if not m:
            continue
        name = m.group(2)
        if name.startswith('<signal handler called>'):
            name = '<signal handler called>'
        res.append({'level': int(m.group(1)), 'name': name})
    return res


def probe_seq():
    '''Which stop this is: the scenario encodes it in the target's
    argument (box_space_id_by_name: len = 40 + seq; lbox_session_exists:
    first argument 4000 + seq).'''
    try:
        f = gdb.newest_frame()
    except gdb.error:
        return None
    for _ in range(3):
        if f is None:
            break
        try:
            if f.name() == 'box_space_id_by_name':
                return int(f.read_var('len')) - 40
            if f.name() == 'lbox_session_exists':
                L = f.read_var('L')
                return int(float(L['base'][0]['n'])) - 4000
        except (gdb.error, ValueError):
            pass
        f = f.older()
    return None


def probe_ljstack(L):
    '''Number of frames lj-stack (luajit_dbg.frames) walks; -1 if it does
    not terminate: frames() trusts every link, a link that does not go
    down loops it forever (lj-stack hangs the same way).'''
    try:
        lv = M.ljbt_cast('lua_State *', int(L, 16))
        n = 0
        last = None
        for link, _ in M.frames(lv):
            n += 1
            if n > 20000 or (last is not None and int(link) >= last):
                return -1
            last = int(link)
        return n
    except Exception:
        return None


class ProbeCommand(gdb.Command):
    def __init__(self):
        super(ProbeCommand, self).__init__('ljbt-probe', gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        argv = gdb.string_to_argv(arg)
        path = argv[0]
        try:
            pc = '0x{:x}'.format(gdb.newest_frame().pc())
        except gdb.error:
            pc = None
        def stage(name):
            # Where a hang happened, if one does.
            with open(path + '.progress', 'a') as fh:
                fh.write('{} pc={}\n'.format(name, pc))
        stage('start')
        rec = {'seq': probe_seq(), 'error': None}
        try:
            th = gdb.selected_thread()
            rec['thread'] = th.num if th else None
            rec['thread_name'] = th.name if th else None
        except gdb.error:
            rec['thread'] = None
        try:
            M.ljbt_reset()
            M.LJBT_OPTS.update(verbose=False, full=False, tail_chain=False,
                               no_tail=False)
            out = []
            M.ljbt_backtrace(None, 256, out, True)
            rec['ljbt'] = json.loads(out[0])
        except Exception:
            rec['ljbt'] = None
            rec['error'] = traceback.format_exc()
        stage('bt')
        try:
            rec['bt'] = probe_parse_bt(gdb.execute('bt -no-filters', False,
                                                   True))
        except gdb.error as e:
            rec['bt'] = []
            rec['bt_error'] = str(e)
        doc = rec['ljbt']
        stage('rest')
        if doc and any(f['kind'] == 'trace' for f in doc['frames']):
            try:
                gdb.execute('disable unwinder global lj-bt-trace', False,
                            True)
                gdb.invalidate_cached_frames()
                rec['bt_raw'] = probe_parse_bt(gdb.execute(
                    'bt -no-filters 64', False, True))
            except gdb.error as e:
                rec['bt_raw_error'] = str(e)
            finally:
                gdb.execute('enable unwinder global lj-bt-trace', False,
                            True)
                gdb.invalidate_cached_frames()
        if doc:
            rec['ljstack'] = dict((s['L'], probe_ljstack(s['L']))
                                  for s in doc['states'])
        if '--filter' in argv:
            try:
                rec['bt_filter'] = gdb.execute('bt', False, True)
            except gdb.error as e:
                rec['bt_filter'] = 'error: {}'.format(e)
        if '--tail-chain' in argv:
            try:
                M.ljbt_reset()
                M.LJBT_OPTS.update(tail_chain=True)
                out = []
                M.ljbt_backtrace(None, 256, out, True)
                rec['ljbt_chain'] = json.loads(out[0])
            except Exception:
                rec['error'] = traceback.format_exc()
            finally:
                M.LJBT_OPTS.update(tail_chain=False)
        if '--text' in argv:
            try:
                rec['text'] = gdb.execute('lj-bt', False, True)
            except gdb.error as e:
                rec['text'] = 'error: {}'.format(e)
        if '--all-fibers' in argv and hasattr(M, 'FiberUnwinder'):
            try:
                rec['all_fibers'] = json.loads(gdb.execute(
                    'lj-bt --all-fibers --json', False, True))
            except Exception:
                rec['all_fibers_error'] = traceback.format_exc()
        if '--gcore-dir' in argv and rec['seq'] is not None:
            seqs = argv[argv.index('--gcore-seqs') + 1].split(',')
            if str(rec['seq']) in seqs:
                core = '{}/core.{}'.format(
                    argv[argv.index('--gcore-dir') + 1], rec['seq'])
                try:
                    gdb.execute('gcore ' + core, False, True)
                    rec['core'] = core
                except gdb.error as e:
                    rec['core_error'] = str(e)
        with open(path, 'a') as fh:
            fh.write(json.dumps(rec) + '\n')


class ProbeTtCommand(gdb.Command):
    '''ljbt-probe-tt PATH: source tarantool-gdb.py once (it needs a live
    process, so it cannot go into the prelude).'''

    def __init__(self):
        super(ProbeTtCommand, self).__init__('ljbt-probe-tt',
                                             gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        if not arg or hasattr(M, 'FiberUnwinder'):
            return
        import gdb.printing  # noqa: F401 (tarantool-gdb.py needs it)
        gdb.execute('source ' + arg, False, True)


ProbeCommand()
ProbeTtCommand()
