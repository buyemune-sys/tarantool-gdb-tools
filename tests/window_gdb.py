# gdb side of the window layer (tests/test_window.py): sourced after
# probe.py.
#
#   ljbt-window FILE SEQS MAXSTEPS
#
# The program is stopped at the mark of the first chain of SEQS: in
# lbox_session_exists, called right before the first step of the chain
# (gen.Chain window=True: its first argument is 2000 + seq).  For each
# chain, in order: `finish` out of the mark, then one instruction at a
# time (`stepi`) through the code of the VM, probed (`ljbt-probe FILE
# --seq SEQ --tag K`), and `finish` out of each C function the VM calls,
# until the chain is in its target (lbox_session_exists again) or
# MAXSTEPS instructions are probed; then `continue` to the mark of the
# next chain.  Probed is every instruction of the code that builds or
# tears down frames (FRAME_CODE), the first one of any other bytecode
# handler (a table lookup is a loop of a hundred instructions over one
# and the same stack).  A line {"window": SEQ, "reached": ..., "steps":
# ..., "insns": ...} after the stops of each chain.

import json
import re

import gdb

import __main__ as M

FRAME_CODE = re.compile(r'lj_BC_(CALL|RET|FUNC|IFUNC|JFUNC|ITER|VARG|'
                        r'ISNEXT)|lj_vm_|lj_vmeta_|lj_cont_|lj_ff_')
MAX_INSNS = 5000


def window_arg():
    '''The first argument of lbox_session_exists in the newest frame,
    None elsewhere.'''
    try:
        f = gdb.newest_frame()
        if f.name() != 'lbox_session_exists':
            return None
        return int(M.probe_number(f.read_var('L')['base'][0]))
    except (gdb.error, ValueError):
        return None


def window_to_mark():
    '''Continue until a mark (2000 <= argument < 3000).'''
    while True:
        gdb.execute('continue', False, True)
        a = window_arg()
        if a is not None and 2000 <= a < 3000:
            return


class WindowCommand(gdb.Command):
    def __init__(self):
        super(WindowCommand, self).__init__('ljbt-window', gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        path, seqs, maxsteps = gdb.string_to_argv(arg)
        maxsteps = int(maxsteps)
        c = M.ljbt_conf()
        for n, seq in enumerate(seqs.split(',')):
            if n:
                window_to_mark()
            gdb.execute('finish', False, True)
            steps, insns, reached, prev = 0, 0, False, None
            while steps < maxsteps and insns < MAX_INSNS:
                f = gdb.newest_frame()
                name = f.name() or ''
                if name == 'lbox_session_exists':
                    reached = True
                    break
                if c.vm_begin <= f.pc() < c.vm_end:
                    if name != prev or FRAME_CODE.match(name):
                        gdb.execute('ljbt-probe {} --seq {} --tag {}'.format(
                            path, seq, steps), False, True)
                        steps += 1
                    prev = name
                    gdb.execute('stepi', False, True)
                    insns += 1
                else:
                    # C code: the VM's helper, or a C function of the chain
                    # (table.sort, fiber.create) that calls on down to the
                    # target, whose breakpoint stops the finish.
                    prev = None
                    gdb.execute('finish', False, True)
            with open(path, 'a') as fh:
                fh.write(json.dumps({'window': int(seq), 'reached': reached,
                                     'steps': steps, 'insns': insns}) + '\n')


WindowCommand()
