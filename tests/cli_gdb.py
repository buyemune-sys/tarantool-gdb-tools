# gdb side of the cli layer (test_cli.py): sourced after probe.py.
#
#   ljbt-cli-run OUT [TT_GDB]
#
# runs tests/lua/cli.lua to the end and, at each numbered stop (see the
# scenario), drives lj-bt through gdb.execute as a user would and appends
# one JSON record per action to OUT.

import json
import os
import traceback

import gdb

import __main__ as M

LAST = {'bp': None}


def cli_on_stop(event):
    # The number, not the object: a temporary breakpoint is gone by the
    # time the loop looks at it.
    bps = getattr(event, 'breakpoints', None) or []
    try:
        LAST['bp'] = bps[0].number if bps else None
    except RuntimeError:
        LAST['bp'] = None


gdb.events.stop.connect(cli_on_stop)


def run(cmd):
    '''(output, error) of a gdb command.'''
    try:
        return gdb.execute(cmd, False, True), None
    except gdb.error as e:
        return None, str(e)


def state():
    '''What lj-bt must not change: the selected thread and frame, and the
    registers of frame 0.'''
    th = gdb.selected_thread()
    f = gdb.selected_frame()
    return {'thread': th.num if th else None, 'frame': f.level(),
            'regs': run('info registers pc sp')[0]}


def lua_lines(text):
    '''The Lua and heuristic lines of lj-bt or of the filtered bt.'''
    res = []
    for line in (text or '').splitlines():
        s = line.strip()
        if '[lua]' in s:
            s = s[s.index('[lua]'):]
            if s.endswith(' ()'):
                s = s[:-3]          # bt prints the (empty) arguments.
            res.append(s)
    return res


class Cli(object):
    def __init__(self, out, tt):
        self.out = out
        self.tt = tt
        self.armed = {}

    def rec(self, n, what, **kw):
        kw.update(n=n, what=what)
        with open(self.out, 'a') as fh:
            fh.write(json.dumps(kw) + '\n')

    def snapshot(self, n, what, args=''):
        # bt first: the lj-bt command would refresh the state for it.
        bt, _ = run('bt')
        raw, _ = run('bt -no-filters')
        text, err = run('lj-bt ' + args)
        js, jerr = run('lj-bt --json ' + args)
        self.rec(n, what, text=text, error=err or jerr,
                 doc=json.loads(js) if js else None, bt=bt, raw=raw,
                 bt_lua=lua_lines(bt), text_lua=lua_lines(text))

    def threads(self, n):
        orig = gdb.selected_thread().num
        for th in gdb.selected_inferior().threads():
            run('thread {}'.format(th.num))
            self.snapshot(n, 'thread', '')
            self.rec(n, 'thread-id', num=th.num, name=th.name)
        run('thread {}'.format(orig))

    def on_marker(self, n):
        if n == 1:
            before = state()
            for args in ['', '-v', '-f', '-n 3', '--no-tail', '--tail-chain',
                         '--json']:
                text, err = run('lj-bt ' + args)
                self.rec(n, 'flag', args=args, text=text, error=err)
            o = self.out + '.o'
            for _ in range(2):
                run('lj-bt -o ' + o)
            self.rec(n, 'flag-o', path=o)
            for args in ['-n', '-n x', '-n 0', '-o']:
                text, err = run('lj-bt ' + args)
                self.rec(n, 'bad-flag', args=args, text=text, error=err)
            seq = [run('lj-bt --json')[0]]
            run('bt')
            seq.append(run('lj-bt --json')[0])
            if self.tt:
                run('lj-bt --all-fibers')
            seq.append(run('lj-bt --json')[0])
            self.rec(n, 'idempotent', docs=seq)
            self.rec(n, 'state', before=before, after=state())
            self.snapshot(n, 'stop')
            self.threads(n)
        elif n == 2:
            with open(os.path.join(os.environ['LJBT_WORKDIR'],
                                   'co.addr')) as fh:
                addr = fh.read().strip()
            self.snapshot(n, 'coroutine', '(lua_State *){}'.format(addr))
        elif n == 3:
            self.snapshot(n, 'deep')
            self.snapshot(n, 'deep-all', '-n 2000')
        elif n in (4, 5):
            self.snapshot(n, 'stop')
        elif n == 6:
            self.arm('lj_vm_floor', 'floor')
        elif n == 7:
            self.arm('lj_ff_math_abs', 'ff')
        elif n == 10:
            self.arm('lj_vm_return', 'vm_return')
        elif n == 8:
            self.arm('iproto_connection_on_input', 'iproto')
        elif n == 9:
            self.sleeper(n)
        elif n == 11:
            self.other(n)

    def arm(self, where, what):
        try:
            bp = gdb.Breakpoint(where, internal=True, temporary=True)
            self.armed[bp.number] = what
        except gdb.error as e:
            self.rec(0, what, error='cannot break at {}: {}'.format(where,
                                                                     e))

    def on_armed(self, what):
        self.snapshot(0, what)
        if what == 'iproto':
            # Stopped in the iproto thread; the tx thread runs no Lua now.
            self.threads(0)

    def sleeper_fid(self):
        for f in M.Cord().fibers():
            if f['name'].string() == 'cli-sleeper':
                return int(f['fid'])
        return None

    def sleeper(self, n):
        # tarantool_gdb.py is loaded here, at a stop: its selection is of
        # this stop (stop 11 checks that it does not outlive it).
        if not self.tt:
            return
        run('ljbt-probe-tt ' + self.tt)
        fid = self.sleeper_fid()
        if fid is None:
            self.rec(n, 'sleeper', error='no cli-sleeper fiber')
            return
        allf, aerr = run('lj-bt --all-fibers --json')
        if aerr:
            self.rec(n, 'all-fibers-error', error=aerr)
        run('tt-fiber {}'.format(fid))
        self.snapshot(n, 'sleeper')
        self.rec(n, 'all-fibers', fid=fid,
                 docs=json.loads(allf) if allf else None)

    def other(self, n):
        # Stopped in cli-other after a continue: the running fiber again;
        # then tt-fiber and a stepi, which keeps the selection.
        if not self.tt:
            return
        self.snapshot(n, 'other')
        # A frame other than #0 selected: lj-bt and --all-fibers keep it.
        run('frame 2')
        before = state()
        run('lj-bt')
        after = state()
        run('lj-bt --all-fibers')
        self.rec(n, 'frame-kept', before=before, after=after,
                 after_all=state())
        run('frame 0')
        fid = self.sleeper_fid()
        if fid is None:
            self.rec(n, 'sleeper-stepi', error='no cli-sleeper fiber')
            return
        run('tt-fiber {}'.format(fid))
        run('stepi')
        self.snapshot(n, 'sleeper-stepi')

    def loop(self):
        cmd = 'run'
        while True:
            out, err = run(cmd)
            cmd = 'continue'
            if err is not None or not gdb.selected_inferior().pid:
                break
            bp = LAST['bp']
            try:
                if bp is not None and bp in self.armed:
                    self.on_armed(self.armed.pop(bp))
                else:
                    n = M.probe_seq()       # 5000 + n - 4000
                    if n is not None:
                        self.on_marker(n - 1000)
            except Exception:
                self.rec(-1, 'exception', error=traceback.format_exc())


class CliRun(gdb.Command):
    def __init__(self):
        super(CliRun, self).__init__('ljbt-cli-run', gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        argv = gdb.string_to_argv(arg)
        Cli(argv[0], argv[1] if len(argv) > 1 else None).loop()


CliRun()
