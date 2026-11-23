# Runs inside the debugger for tests/manifest/check.py: loads files into
# gdb or lldb and records what each one registered, read back from the
# debugger itself (gdb: `help all`; lldb: `command script list`).
#
#   gdb -batch -nx -x probe.py                     (plan in $MANIFEST_PLAN)
#   lldb --batch --no-lldbinit -o 'command script import probe.py'
#
# The plan (JSON): binary, the file of the target; sources, the files to
# load in order; out, where the result goes.  The result (JSON):
# {"loads": [{"file": F, "commands": [...], "parameters": [...],
# "output": text, "error": text or null}, ...], "fatal": text or null}:
# the commands and parameters each file added to those of the debugger
# before it.
#
# gdb runs this file and the files it sources in one namespace
# (__main__): every name here starts with mfprobe_.
import json as mfprobe_json
import os as mfprobe_os
import re as mfprobe_re
import traceback as mfprobe_traceback


def mfprobe_write(plan, res):
    with open(plan['out'], 'w') as fh:
        mfprobe_json.dump(res, fh, indent=1, sort_keys=True)


def mfprobe_gdb_names(gdb):
    '''(commands, parameters) gdb has: the full names of `help all`.  A
    parameter is a "set X" with a "show X"; "function X" lines are
    convenience functions.'''
    text = gdb.execute('help all', False, True)
    names = set()
    for line in text.split('\n'):
        if ' -- ' not in line:
            continue
        # "backtrace, where, bt -- ...": the command, then its aliases.
        name = ' '.join(line.split(' -- ', 1)[0].split(',')[0].split())
        if name:
            names.add(name)
    params = set(n[4:] for n in names
                 if n.startswith('set ') and 'show ' + n[4:] in names)
    cmds = set(n for n in names
               if not n.startswith('function ') and
               not (n.startswith(('set ', 'show ')) and
                    n.split(' ', 1)[1] in params))
    return cmds, params


def mfprobe_gdb():
    import gdb
    plan = mfprobe_json.load(open(mfprobe_os.environ['MANIFEST_PLAN']))
    res = {'loads': [], 'fatal': None}
    try:
        for cmd in ('set pagination off', 'set width 0', 'set confirm off'):
            gdb.execute(cmd)
        gdb.execute('file ' + plan['binary'], False, True)
        before = mfprobe_gdb_names(gdb)
        for path in plan['sources']:
            err = None
            try:
                out = gdb.execute('source ' + path, False, True)
            except gdb.error as e:
                out, err = '', str(e) or 'error'
            after = mfprobe_gdb_names(gdb)
            res['loads'].append({
                'file': mfprobe_os.path.basename(path),
                'commands': sorted(after[0] - before[0]),
                'parameters': sorted(after[1] - before[1]),
                'output': out, 'error': err})
            before = after
    except Exception:
        res['fatal'] = mfprobe_traceback.format_exc()
    mfprobe_write(plan, res)


mfprobe_LLDB_USER = mfprobe_re.compile(r'^\s+(\S+)\s+--')


def mfprobe_lldb_names(ci, lldb):
    '''The user commands lldb has (`command script list`); lldb has no
    parameters for scripts to add.'''
    r = lldb.SBCommandReturnObject()
    ci.HandleCommand('command script list', r)
    cmds = set()
    for line in (r.GetOutput() or '').split('\n'):
        m = mfprobe_LLDB_USER.match(line)
        if m:
            cmds.add(m.group(1))
    return cmds, set()


def __lldb_init_module(debugger, internal_dict):
    import lldb
    plan = mfprobe_json.load(open(mfprobe_os.environ['MANIFEST_PLAN']))
    res = {'loads': [], 'fatal': None}
    try:
        debugger.SetAsync(False)
        ci = debugger.GetCommandInterpreter()
        r = lldb.SBCommandReturnObject()
        ci.HandleCommand('target create ' + plan['binary'], r)
        if not r.Succeeded():
            raise RuntimeError(r.GetError())
        before = mfprobe_lldb_names(ci, lldb)
        for path in plan['sources']:
            r = lldb.SBCommandReturnObject()
            ci.HandleCommand('command script import ' + path, r)
            after = mfprobe_lldb_names(ci, lldb)
            res['loads'].append({
                'file': mfprobe_os.path.basename(path),
                'commands': sorted(after[0] - before[0]),
                'parameters': sorted(after[1] - before[1]),
                'output': r.GetOutput() or '',
                'error': None if r.Succeeded() else
                (r.GetError() or 'error').strip()})
            before = after
    except Exception:
        res['fatal'] = mfprobe_traceback.format_exc()
    mfprobe_write(plan, res)


try:
    import gdb as mfprobe_in_gdb  # noqa: F401
except ImportError:
    pass
else:
    mfprobe_gdb()
