# Runs inside lldb: lldb --batch --no-lldbinit -o 'command script import
# driver_lldb.py', the plan in $DBG_PLAN.  The lldb twin of driver.py:
# the same plan and the same result (see driver.py), so that harness.py
# and the tests run in either debugger (DBG_DEBUGGER=lldb).
#
# The plan speaks gdb; what the tests use of it is translated here:
#   break LOC [if COND], condition N COND, delete, continue, next,
#   frame N, set var X = Y, source F (command script import; the gdb
#   helpers of the tests have lldb versions below: oracle.py,
#   hide_enums.py, enumcheck.py), the two thread switches of
#   test_luajit.py (gdb Python), and the convenience functions of
#   oracle.py and $_streq, in expressions and in the arguments of
#   commands.  A breakpoint condition is evaluated here, not by lldb.
#   Anything else goes to lldb as is.
#
# An error of a command is its message as luajit_gdb.py gives it, with
# lldb's "error: Failed to execute command `X`: " taken off; a Python
# exception the extension let out is "Error occurred in Python: ..." as
# in gdb.
#
# The core (gcore in the plan) is lldb's: on Linux it writes minidumps
# only.  The style is modified-memory (DBG_LLDB_CORE: full, stack): as
# in a kernel core or a gcore one, the read-only file-backed pages
# (.rodata) are not there.  gdb cannot write it instead: lldb-server
# cannot detach and leave the process stopped ("Stays stopped not
# supported by this target"; it takes a SIGSTOP for its own).
#
# lldb-server cannot allocate memory in tarantool for an expression: it
# runs an mmap in the inferior and takes the stop of the SIGALRM that
# tarantool's clock keeps pending for its end, the address is then the
# syscall number, and lldb keeps that page for good.  The driver gets
# good pages first (prime()).
import io
import json
import os
import re
import sys
import traceback

import lldb

PY_ERROR = 'Error occurred in Python'
STOP_REASONS = (lldb.eStopReasonBreakpoint, lldb.eStopReasonSignal,
                lldb.eStopReasonException, lldb.eStopReasonWatchpoint)

# The thread switches of test_luajit.py, gdb Python: the main thread
# (its tid is the pid) or another one.
GDB_SWITCH = re.compile(r'^python inf = gdb\.selected_inferior\(\); '
                        r'\[t for t in inf\.threads\(\) if '
                        r'\(t\.ptid\[1\] (==|!=) inf\.pid\)\]\[0\]'
                        r'\.switch\(\)$')


class Error(Exception):
    pass


class Driver(object):

    def __init__(self, debugger, plan):
        self.dbg = debugger
        self.ci = debugger.GetCommandInterpreter()
        self.plan = plan
        self.target = None
        self.conds = {}         # breakpoint id: a condition of ours
        self.hidden = set()

    # Commands. ###################################################

    def handle(self, cmd):
        '''Run an lldb command: (out, err).  Python output and tracebacks
        of the command (luajit_gdb.py writes to sys.stdout) are taken.'''
        res = lldb.SBCommandReturnObject()
        out, err = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            self.ci.HandleCommand(cmd, res)
        finally:
            sys.stdout, sys.stderr = saved
        text = out.getvalue() + (res.GetOutput() or '')
        stderr = err.getvalue()
        if 'Traceback (most recent call last)' in stderr:
            last = [s for s in stderr.split('\n') if s.strip()][-1]
            return text, '{}: {}'.format(PY_ERROR, last)
        if not res.Succeeded():
            msg = (res.GetError() or 'failed').strip()
            msg = re.sub(r'^error: ', '', msg)
            msg = re.sub(r'^Failed to execute command `[^`]*`: ', '', msg)
            return text, msg
        return text, None

    def ex(self, cmd):
        '''A plan command; gdb.error is our Error.'''
        if cmd.startswith('eval:'):
            return self.value(cmd[5:])
        m = GDB_SWITCH.match(cmd)
        if m:
            return self.switch_thread(m.group(1) == '==')
        words = cmd.split(None, 1)
        head = words[0] if words else ''
        rest = words[1] if len(words) > 1 else ''
        if head in ('break', 'tbreak', 'b'):
            return self.set_break(rest, head == 'tbreak')
        if head == 'condition':
            num, cond = (rest.split(None, 1) + [''])[:2]
            self.conds[int(num)] = cond or None
            return ''
        if head == 'delete' and not rest:
            self.target.DeleteAllBreakpoints()
            self.conds.clear()
            return ''
        if head in ('continue', 'c'):
            return self.resume(self.process().Continue)
        if head in ('next', 'n'):
            self.process().GetSelectedThread().StepOver()
            return ''
        if head == 'frame' and rest.isdigit():
            th = self.process().GetSelectedThread()
            if not th.SetSelectedFrame(int(rest)).IsValid():
                raise Error('No frame at level {}.'.format(rest))
            return ''
        if head == 'set' and rest.startswith('var '):
            self.evaluate(rest[4:])
            return ''
        if head == 'source':
            return self.source(rest)
        if head == 'dbg-enum-check':
            return self.enum_check()
        if head == 'python':
            raise Error('driver_lldb: gdb Python: ' + rest)
        if re.search(r'\$\w+\(', cmd):
            # gdb evaluates a convenience function in the argument of
            # an extension command; lldb has none: put in its value.
            cmd = self.substitute(cmd)[0]
        out, err = self.handle(cmd)
        if err is not None:
            raise Error(err)
        return out

    def try_ex(self, cmd):
        try:
            return {'out': self.ex(cmd), 'err': None}
        except Error as e:
            return {'out': '', 'err': str(e)}
        except Exception:
            return {'out': '', 'err': 'driver: ' + traceback.format_exc()}

    def source(self, path):
        name = os.path.basename(path)
        if name == 'oracle.py':
            return ''               # Its functions are in evaluate().
        if name == 'hide_enums.py':
            return self.hide_enums()
        if name == 'enumcheck.py':
            return ''               # dbg-enum-check is ours.
        out, err = self.handle('command script import ' + path)
        if err is not None:
            raise Error(err)
        return out

    # Process. ####################################################

    def process(self):
        p = self.target.GetProcess()
        if not p.IsValid():
            raise Error('The program is not being run.')
        return p

    def set_break(self, spec, temporary):
        m = re.match(r'^(\S+)(?:\s+if\s+(.*))?$', spec.strip())
        if not m:
            raise Error('break: ' + spec)
        loc, cond = m.group(1), m.group(2)
        fl = re.match(r'^(.+):(\d+)$', loc)
        if fl:
            bp = self.target.BreakpointCreateByLocation(fl.group(1),
                                                        int(fl.group(2)))
        else:
            bp = self.target.BreakpointCreateByName(loc)
        if not bp.IsValid() or bp.GetNumLocations() == 0:
            self.target.BreakpointDelete(bp.GetID())
            # gdb in batch mode answers "no" to a pending breakpoint.
            return 'Function "{}" not defined.'.format(loc)
        bp.SetOneShot(temporary)
        if cond:
            self.conds[bp.GetID()] = cond
        return 'Breakpoint {} at {:#x}: {}.'.format(
            bp.GetID(), bp.GetLocationAtIndex(0).GetLoadAddress(), loc)

    def stopped_at(self):
        '''The breakpoint ids the selected thread stopped at.'''
        th = self.process().GetSelectedThread()
        if th.GetStopReason() != lldb.eStopReasonBreakpoint:
            return []
        return [th.GetStopReasonDataAtIndex(i)
                for i in range(0, th.GetStopReasonDataCount(), 2)]

    def select_stopped_thread(self):
        p = self.process()
        for th in p:
            if th.GetStopReason() in STOP_REASONS:
                p.SetSelectedThread(th)
                return

    def resume(self, how):
        '''Run on (launch, continue) to a stop whose condition holds.'''
        how()
        while True:
            p = self.process()
            if p.GetState() != lldb.eStateStopped:
                return 'process {}'.format(
                    self.dbg.StateAsCString(p.GetState()))
            self.select_stopped_thread()
            ids = self.stopped_at()
            conds = [self.conds.get(i) for i in ids]
            if ids and all(c is not None for c in conds) and \
               not any(self.truth(c) for c in conds):
                p.Continue()
                continue
            th = p.GetSelectedThread()
            return 'stopped: {}\n'.format(th.GetStopDescription(256))

    def truth(self, cond):
        try:
            return bool(self.integer(self.evaluate(cond)))
        except Error:
            return True             # gdb stops on an error of a condition

    def launch(self):
        plan = self.plan
        info = lldb.SBLaunchInfo([str(a) for a in plan.get('args', [])])
        if plan.get('cwd'):
            info.SetWorkingDirectory(plan['cwd'])
        env = dict((k, v) for k, v in os.environ.items()
                   if not k.startswith('DBG_'))
        env.update(plan.get('env', {}))
        info.SetEnvironmentEntries(['{}={}'.format(k, v)
                                    for k, v in sorted(env.items())], False)
        info.SetLaunchFlags(info.GetLaunchFlags() |
                            lldb.eLaunchFlagDisableASLR)
        # Tarantool's clock keeps a SIGALRM pending in a stopped process:
        # passed, it makes `next` stop where it began (the handler runs,
        # the breakpoint at the pc is hit again).
        self.handle('process handle SIGALRM --pass false --stop false '
                    '--notify false')

        def go():
            err = lldb.SBError()
            self.target.Launch(info, err)
            if err.Fail():
                raise Error('launch: ' + str(err))
        return self.resume(go)

    def switch_thread(self, main):
        p = self.process()
        for th in p:
            if (th.GetThreadID() == p.GetProcessID()) == main:
                p.SetSelectedThread(th)
                return ''
        raise Error('no such thread')

    def gcore(self, path):
        '''Write the core (a minidump, see the header).'''
        style = os.environ.get('DBG_LLDB_CORE', 'modified-memory')
        out, err = self.handle('process save-core --plugin-name=minidump '
                               '--style {} {}'.format(style, path))
        if err is not None:
            raise Error(err)
        return out

    PRIME = ((0x1000, lldb.ePermissionsReadable | lldb.ePermissionsWritable),
             (0x1000, lldb.ePermissionsReadable |
              lldb.ePermissionsExecutable),
             (0x1000, lldb.ePermissionsReadable | lldb.ePermissionsWritable |
              lldb.ePermissionsExecutable),
             (0x1000, lldb.ePermissionsReadable),
             (0x200000, lldb.ePermissionsReadable |
              lldb.ePermissionsWritable))

    def prime(self):
        '''Allocate until lldb has a mapped page of each kind, leave the
        bad ones reserved in full (see the header).'''
        p = self.target.GetProcess()
        if p.GetPluginName() != 'gdb-remote' or getattr(self, 'primed', 0):
            return
        self.primed = True
        for size, perm in self.PRIME:
            for _ in range(20):
                err = lldb.SBError()
                addr = p.AllocateMemory(size, perm, err)
                if err.Fail():
                    break
                info = lldb.SBMemoryRegionInfo()
                if p.GetMemoryRegionInfo(addr, info).Success() and \
                   info.IsMapped():
                    p.DeallocateMemory(addr)
                    break

    # Expressions. ################################################

    def frame(self):
        p = self.target.GetProcess()
        return p.GetSelectedThread().GetSelectedFrame()

    def substitute(self, expr):
        '''expr with the calls of our $functions replaced by their
        values: (text, value of the whole expr if it is one call).'''
        m = re.search(r'\$(\w+)\(', expr)
        while m:
            name = m.group(1)
            fn = getattr(self, 'fn_' + name, None)
            if fn is None:
                raise Error('driver_lldb: no function ${}'.format(name))
            end, args = split_call(expr, m.end())
            vals = [a[1:-1] if a.startswith('"') else self.evaluate(a)
                    for a in args]
            res = int(fn(*vals))
            if m.start() == 0 and end == len(expr):
                return expr, res
            expr = '{}({}){}'.format(expr[:m.start()], res, expr[end:])
            m = re.search(r'\$(\w+)\(', expr)
        return expr, None

    def evaluate(self, expr):
        '''An SBValue (or a Python int) of a gdb expression.'''
        expr, res = self.substitute(expr.strip())
        if res is not None:
            return res
        word = re.match(r'^(?:\w+::)?(\w+)$', expr)
        if word and word.group(1) in self.hidden:
            raise Error('No symbol "{}" in current context.'.format(expr))
        self.prime()
        frame = self.frame()
        v = frame.EvaluateExpression(expr)
        if v.GetError().Fail() and 'out of scope' in \
                (v.GetError().GetCString() or ''):
            # A global (tarantool_L) from a frame of assembly or of a
            # library with no debug info: lldb finds it from another.
            for f in frame.GetThread():
                v = f.EvaluateExpression(expr)
                if not v.GetError().Fail():
                    break
        if not v.IsValid():
            raise Error('{}: invalid expression'.format(expr))
        if v.GetError().Fail():
            raise Error(str(v.GetError().GetCString()).strip())
        return v

    def integer(self, v):
        if isinstance(v, int):
            return v
        t = v.GetType().GetCanonicalType()
        if t.IsPointerType():
            return v.GetValueAsUnsigned()
        if t.GetTypeClass() == lldb.eTypeClassEnumeration:
            return v.GetValueAsSigned()
        basic = t.GetBasicType()
        if basic in (lldb.eBasicTypeFloat, lldb.eBasicTypeDouble,
                     lldb.eBasicTypeLongDouble):
            return int(float(v.GetValue()))
        if t.GetTypeClass() != lldb.eTypeClassBuiltin:
            raise ValueError('not a scalar')
        if basic in (lldb.eBasicTypeUnsignedChar, lldb.eBasicTypeUnsignedShort,
                     lldb.eBasicTypeUnsignedInt, lldb.eBasicTypeUnsignedLong,
                     lldb.eBasicTypeUnsignedLongLong, lldb.eBasicTypeBool,
                     lldb.eBasicTypeUnsignedInt128):
            return v.GetValueAsUnsigned()
        return v.GetValueAsSigned()

    def value(self, expr):
        '''"eval:": an integer if it has one, else the text.'''
        v = self.evaluate(expr)
        try:
            return self.integer(v)
        except ValueError:
            return str(v)

    # oracle.py: the same functions, over SBValue.

    def ref(self, v, kind):
        names = [v.GetChildAtIndex(i).GetName()
                 for i in range(v.GetNumChildren())]
        c = v.GetChildMemberWithName(kind + '64' if kind + '64' in names
                                     else kind + '32')
        err = lldb.SBError()
        n = c.GetValueAsUnsigned(err, 0)
        if err.Fail() or not c.IsValid():
            raise Error('cannot read {}: {}'.format(kind, err))
        return n

    def gcobj(self, addr):
        tp = self.target.FindFirstType('GCobj')
        return self.target.CreateValueFromAddress(
            'o', lldb.SBAddress(addr, self.target), tp)

    def fn_dbg_gclen(self, head, ring=0):
        first = self.ref(head, 'gcptr')
        if not first:
            return 0

        def nextgc(p):
            return self.ref(self.gcobj(p).GetChildMemberWithName('gch')
                            .GetChildMemberWithName('nextgc'), 'gcptr')
        if self.integer(ring):
            last, p, n = first, nextgc(first), 1
            while p != last:
                n += 1
                p = nextgc(p)
            return n
        n, p = 0, first
        while p:
            n += 1
            p = nextgc(p)
        return n

    def fn_dbg_mref(self, ref):
        return self.ref(ref, 'ptr')

    def fn_dbg_gcref(self, ref):
        return self.ref(ref, 'gcptr')

    def fn_dbg_ir_isins(self, T, ref):
        REF_BIAS = 0x8000
        ref = self.integer(ref)
        T = T.Dereference() if T.GetType().IsPointerType() else T
        if ref >= REF_BIAS:
            return int(ref < T.GetChildMemberWithName('nins')
                       .GetValueAsUnsigned())
        two = ['IR_KNUM', 'IR_KINT64']
        if self.target.FindFirstType('GCRef').GetByteSize() == 8:
            two += ['IR_KGC', 'IR_KPTR', 'IR_KKPTR']
        two = set(self.integer(self.evaluate('IROp::' + n)) for n in two)
        ir = T.GetChildMemberWithName('ir')
        tp = ir.GetType().GetPointeeType()
        base = ir.GetValueAsUnsigned()
        r = T.GetChildMemberWithName('nk').GetValueAsUnsigned()
        while r < REF_BIAS:
            if r == ref:
                return 1
            ins = self.target.CreateValueFromAddress(
                'ir', lldb.SBAddress(base + r * tp.GetByteSize(),
                                     self.target), tp)
            o = ins.GetChildMemberWithName('o').GetValueAsUnsigned()
            r += 2 if o in two else 1
        return 0

    def fn__streq(self, a, b):
        err = lldb.SBError()
        s = self.process().ReadCStringFromMemory(self.integer(a), 4096, err)
        if err.Fail():
            raise Error(str(err))
        return int(s == b)

    # hide_enums.py and enumcheck.py for lldb. ###################

    def hide_enums(self):
        '''Make the members of $DBG_HIDE_ENUMS unknown to lldb as
        luajit_gdb.py asks for them (an expression, the enum members of a
        type, the enum types of a module), as if the debug info had not
        their enums (tarantool/tarantool#13094).'''
        hidden = set(os.environ.get('DBG_HIDE_ENUMS', '').split(',')) - {''}
        self.hidden |= hidden
        if getattr(lldb, '_dbg_hidden', None) is not None:
            lldb._dbg_hidden |= hidden
            return ''
        lldb._dbg_hidden = set(hidden)
        evaluate = lldb.SBFrame.EvaluateExpression
        get_members = lldb.SBType.GetEnumMembers
        get_types = lldb.SBModule.GetTypes

        def hidden_name(expr):
            m = re.match(r'^\s*(?:\w+::)?(\w+)\s*$', expr)
            return m and m.group(1) in lldb._dbg_hidden

        def hiding_evaluate(frame, expr, *args):
            if hidden_name(expr):
                return evaluate(frame, '__dbg_hidden_' + expr.split(':')[-1],
                                *args)
            return evaluate(frame, expr, *args)

        def hiding_members(tp):
            members = get_members(tp)
            if not any(members.GetTypeEnumMemberAtIndex(i).GetName() in
                       lldb._dbg_hidden for i in range(members.GetSize())):
                return members
            # The whole enum goes, as in DWARF without it.
            return lldb.SBTypeEnumMemberList()

        def hiding_types(module, mask=lldb.eTypeClassAny):
            types = get_types(module, mask)
            out = lldb.SBTypeList()
            for i in range(types.GetSize()):
                t = types.GetTypeAtIndex(i)
                if t.GetTypeClass() == lldb.eTypeClassEnumeration and \
                   hiding_members(t).GetSize() == 0:
                    continue
                out.Append(t)
            return out
        lldb.SBFrame.EvaluateExpression = hiding_evaluate
        lldb.SBType.GetEnumMembers = hiding_members
        lldb.SBModule.GetTypes = hiding_types
        return ''

    def enum_check(self):
        '''dbg-enum-check of enumcheck.py.'''
        # lldb names a module after its file: look for the extension by
        # what it has, the file may be named otherwise.
        mods = [m for m in list(sys.modules.values())
                if getattr(m, 'ENUM_FALLBACK', None) is not None and
                getattr(m, 'EnumBasedList', None) is not None]
        if not mods:
            raise Error('luajit_gdb.py is not loaded')
        m = mods[-1]
        out = {}
        for v in vars(m).values():
            if not isinstance(v, m.EnumBasedList):
                continue
            name = v._EnumBasedList__max_enum_member
            maxv = m.dbg.create_enum_value(name, v._EnumBasedList__enum_name)
            out[name] = {
                'dwarf': [str(m.dbg.cast_typeof(maxv, i))
                          for i in range(int(maxv))],
                'fallback': m.ENUM_FALLBACK.get(name, '').split(),
            }
        return json.dumps(out) + '\n'

    # The plan. ###################################################

    def run(self):
        plan = self.plan
        res = {'load': '', 'run': '', 'setup': [], 'cmds': {}, 'fatal': None}
        try:
            settings = ['settings set auto-confirm true',
                        'settings set target.load-script-from-symbol-file '
                        'false',
                        'settings set stop-line-count-before 0',
                        'settings set stop-line-count-after 0']
            for s in settings:
                self.handle(s)
            load = []
            if plan.get('deferred'):
                for s in plan['sources']:
                    load.append(self.try_ex('source ' + s))
            core = plan.get('core')
            err = lldb.SBError()
            if core:
                self.target = self.dbg.CreateTarget(plan['binary'])
                p = self.target.LoadCore(core, err)
                load.append({'out': '', 'err': None if p.IsValid()
                             else 'core-file: {}'.format(err)})
            else:
                self.target = self.dbg.CreateTarget(plan['binary'])
                load.append({'out': '', 'err': None
                             if self.target.IsValid() else 'file: invalid'})
            if not plan.get('deferred'):
                for s in plan['sources']:
                    load.append(self.try_ex('source ' + s))
            res['load'] = ''.join(r['out'] for r in load)
            res['load_err'] = [r['err'] for r in load if r['err']]
            if not core:
                for b in plan.get('breaks', []):
                    out = self.ex(b)
                    if b.split()[0] in ('break', 'tbreak') and \
                            'reakpoint ' not in out:
                        raise RuntimeError('{}: {}'.format(b, out))
                res['run'] = self.launch()
                for c in plan.get('setup', []):
                    r = self.try_ex(c)
                    res['setup'].append([c, r['out'], r['err']])
            for c in plan.get('prep', []):
                r = self.try_ex(c)
                res['setup'].append([c, r['out'], r['err']])
            for entry in plan['cmds']:
                key, c = entry[0], entry[1]
                if len(entry) > 2:
                    try:
                        run = bool(self.integer(self.evaluate(entry[2])))
                    except (Error, ValueError):
                        run = False
                    if not run:
                        res['cmds'][key] = {'out': None, 'err': None,
                                            'skipped': True}
                        continue
                res['cmds'][key] = self.try_ex(c)
            if plan.get('gcore'):
                res['gcore'] = self.gcore(plan['gcore'])
        except Exception:
            res['fatal'] = traceback.format_exc()
        finally:
            with open(plan['out'], 'w') as fh:
                json.dump(res, fh)
            try:
                p = self.target.GetProcess() if self.target else None
                if p and p.IsValid() and not plan.get('core'):
                    p.Kill()
            except Exception:
                pass


def split_call(s, start):
    '''The end of a call whose arguments start at s[start] (after "("),
    and its top-level arguments.'''
    depth, i, args, cur, quote = 0, start, [], '', None
    while i < len(s):
        ch = s[i]
        if quote:
            cur += ch
            if ch == '\\':
                cur += s[i + 1]
                i += 1
            elif ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            cur += ch
        elif ch == '(':
            depth += 1
            cur += ch
        elif ch == ')':
            if depth == 0:
                if cur.strip():
                    args.append(cur.strip())
                return i + 1, args
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            args.append(cur.strip())
            cur = ''
        else:
            cur += ch
        i += 1
    raise Error('unbalanced: ' + s)


def __lldb_init_module(debugger, internal_dict):
    debugger.SetAsync(False)
    with open(os.environ['DBG_PLAN']) as fh:
        plan = json.load(fh)
    Driver(debugger, plan).run()
