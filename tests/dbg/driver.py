# Runs inside gdb: gdb -batch -nx -x driver.py, the plan in $DBG_PLAN.
#
# The plan (JSON, written by harness.py):
#   binary     the tarantool binary
#   core       a core file: post-mortem mode, no process
#   args, cwd  the inferior's arguments and working directory (live)
#   env        extra inferior environment (live)
#   sources    gdb scripts to source, in order
#   deferred   source them before `file`, so that they wait for the
#              objfile (the deferred loading of luajit_dbg.py)
#   breaks     commands that set the stop (live): `break`, `condition`...
#   setup      commands after the stop, live only (`next`, `set var`)
#   prep       commands of both modes, before cmds (`set $x = ...`)
#   cmds       [[key, command(, cond)], ...]; "eval:<expr>" records the
#              value of a gdb expression (an integer if it has one); a
#              command with a cond runs only if that expression is true
#   gcore      write a core file there after cmds (live)
#   out        where the result goes
#
# The result (JSON): load, run: output of sourcing and of the run;
# setup: [[cmd, out, err]]; cmds: {key: {"out": text, "err": text}};
# fatal: a traceback if the plan itself broke.  A command error is not
# fatal: "err" holds its message, "Error occurred in Python" in it means
# a Python exception escaped the extension.
import json
import os
import traceback

import gdb


def ex(cmd):
    return gdb.execute(cmd, False, True)


def try_ex(cmd):
    try:
        if cmd.startswith('eval:'):
            v = gdb.parse_and_eval(cmd[5:])
            try:
                return {'out': int(v), 'err': None}
            except (gdb.error, ValueError, TypeError):
                return {'out': str(v), 'err': None}
        return {'out': ex(cmd), 'err': None}
    except gdb.error as e:
        return {'out': '', 'err': str(e)}
    except Exception:
        return {'out': '', 'err': 'driver: ' + traceback.format_exc()}


def main():
    with open(os.environ['DBG_PLAN']) as fh:
        plan = json.load(fh)
    res = {'load': '', 'run': '', 'setup': [], 'cmds': {}, 'fatal': None}
    try:
        for s in ('set pagination off', 'set confirm off', 'set width 0',
                  'set height 0', 'set print pretty off',
                  'set python print-stack full',
                  'set debuginfod enabled off'):
            try:
                ex(s)
            except gdb.error:
                pass            # An older gdb without debuginfod.
        load = []
        if plan.get('deferred'):
            for s in plan['sources']:
                load.append(try_ex('source ' + s))
        load.append(try_ex('file ' + plan['binary']))
        if plan.get('core'):
            load.append(try_ex('core-file ' + plan['core']))
        if not plan.get('deferred'):
            for s in plan['sources']:
                load.append(try_ex('source ' + s))
        res['load'] = ''.join(r['out'] for r in load)
        res['load_err'] = [r['err'] for r in load if r['err']]
        if not plan.get('core'):
            if plan.get('cwd'):
                ex('cd ' + plan['cwd'])
            for k, v in sorted(plan.get('env', {}).items()):
                ex('set environment {}={}'.format(k, v))
            ex('set args ' + ' '.join(plan.get('args', [])))
            for b in plan.get('breaks', []):
                out = ex(b)
                # batch mode answers "no" to a pending breakpoint.
                if b.split()[0] in ('break', 'tbreak') and \
                        'reakpoint ' not in out:
                    raise RuntimeError('{}: {}'.format(b, out))
            res['run'] = ex('run')
            for c in plan.get('setup', []):
                r = try_ex(c)
                res['setup'].append([c, r['out'], r['err']])
        for c in plan.get('prep', []):
            r = try_ex(c)
            res['setup'].append([c, r['out'], r['err']])
        for entry in plan['cmds']:
            key, c = entry[0], entry[1]
            if len(entry) > 2:
                # [key, cmd, cond]: only if cond, a gdb expression, holds.
                try:
                    run = bool(int(gdb.parse_and_eval(entry[2])))
                except gdb.error:
                    run = False
                if not run:
                    res['cmds'][key] = {'out': None, 'err': None,
                                        'skipped': True}
                    continue
            res['cmds'][key] = try_ex(c)
        if plan.get('gcore'):
            ex('gcore ' + plan['gcore'])
    except Exception:
        res['fatal'] = traceback.format_exc()
    finally:
        with open(plan['out'], 'w') as fh:
            json.dump(res, fh)
        try:
            if not plan.get('core'):
                ex('kill')
        except gdb.error:
            pass


main()
