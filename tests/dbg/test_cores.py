# The stacks of the threads of tarantool in a core: gcore, gcore-pages
# (gcore_pages_gdb.py) and the kernel's core, all of the same stop.
#
# Since glibc 2.42 the guard page of a thread stack is installed with
# madvise(MADV_GUARD_INSTALL) (Linux 6.13+) inside the stack mapping, at
# its start, instead of a PROT_NONE mapping of its own; it cannot be
# read (EIO from /proc/PID/mem).  gcore of gdb before 18.1 gives up on a
# mapping at its first read that fails, so every thread stack but the
# main one is zeroes in its core (gdb bug 33855, fixed in gdb 18.1).
#
#   test_guard_pages  the cause: the stack mappings of the threads have
#                     unreadable pages where glibc >= 2.42 runs on
#                     Linux >= 6.13, and nowhere else
#   test_gcore        gdb's gcore keeps every stack; an expected failure
#                     where the stacks have such pages and gdb is older
#                     than 18.1 (the reason goes to stderr)
#   test_gcore_pages  gcore-pages keeps every stack, everywhere, and
#                     reads pages again only where gcore lost them
#   test_kernel_core  the kernel's core keeps every stack; skipped, with
#                     the reason, where none can be taken (core_pattern
#                     pipes to a program, or the hard RLIMIT_CORE is 0)
#
# A stack is kept if the 1 KiB above $sp and the functions of the
# backtrace are the live ones; in the kernel's core, taken after the
# threads ran again (`signal SIGQUIT`), if the 1 KiB is not zeroes and
# the backtrace goes down to the live outermost frame.
#
# DBG_CORES_SUMMARY=FILE writes the facts and the results as JSON.

import glob
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest

import harness as H

STACKS = os.path.join(H.HERE, 'stacks.py')
GCORE_PAGES = H.GCORE_PAGES if H.GCORE_PAGES != 'plain' else \
    os.path.join(H.ROOT, 'gcore_pages_gdb.py')
SUMMARY = os.environ.get('DBG_CORES_SUMMARY')

# box.cfg starts the wal, iproto and applier threads, fio the coio ones;
# strip_core (the default) leaves the memtx arena out of the cores.
SCRIPT = '''\
box.cfg{log = 'tarantool.log'}
require('fio').stat('.')
print('stop')
os.exit(0)
'''


def version(s):
    m = re.search(r'(\d+)\.(\d+)', s)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def os_name():
    try:
        with open('/etc/os-release') as fh:
            for line in fh:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"')
    except IOError:
        pass
    return 'unknown'


GLIBC = os.confstr('CS_GNU_LIBC_VERSION').split()[-1]
KERNEL = os.uname().release
# glibc and Linux that put the guard page inside the stack mapping.
GUARD_IN_STACK = version(GLIBC) >= (2, 42) and version(KERNEL) >= (6, 13)
GDB_FIX = (18, 1)
BUG = 'gdb bug 33855, fixed in gdb 18.1 (commit c1da013915)'


def kernel_core():
    '''(core_pattern, None) if the kernel can write a core here, else
    (None, the reason).'''
    try:
        with open('/proc/sys/kernel/core_pattern') as fh:
            pat = fh.read().strip()
    except IOError as e:
        return None, 'no core_pattern: {}'.format(e)
    if pat.startswith('|'):
        return None, 'kernel.core_pattern pipes cores to {}'.format(
            pat[1:].split()[0])
    if resource.getrlimit(resource.RLIMIT_CORE)[1] == 0:
        return None, 'the hard RLIMIT_CORE is 0'
    return pat, None


def core_glob(pat, pid, cwd):
    '''The glob of the core file the kernel writes for pid: %p and %P are
    the pid, any other specifier matches anything.'''
    out, i, has_pid = '', 0, False
    while i < len(pat):
        if pat[i] == '%' and i + 1 < len(pat):
            s = pat[i + 1]
            out += '%' if s == '%' else str(pid) if s in 'pP' else '*'
            has_pid = has_pid or s in 'pP'
            i += 2
        else:
            out += glob.escape(pat[i])
            i += 1
    with open('/proc/sys/kernel/core_uses_pid') as fh:
        if fh.read().strip() == '1' and not has_pid:
            out += '.{}'.format(pid)
    return os.path.join(cwd, out)


def raise_core_limit():
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))


def run_gdb(wd, plan, tag, preexec=None, timeout=180):
    '''Run driver.py with plan, return its result (as harness.py).'''
    plan = dict(plan, out=os.path.join(wd, tag + '.json'))
    pfile = os.path.join(wd, tag + '.plan.json')
    with open(pfile, 'w') as fh:
        json.dump(plan, fh, indent=1)
    p = subprocess.run([H.GDB, '-batch', '-nx', '-x', H.DRIVER],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, universal_newlines=True,
                       timeout=timeout, cwd=wd, preexec_fn=preexec,
                       env=dict(os.environ, DBG_PLAN=pfile))
    with open(os.path.join(wd, tag + '.gdb.log'), 'w') as fh:
        fh.write(p.stdout)
    try:
        with open(plan['out']) as fh:
            res = json.load(fh)
    except (IOError, ValueError):
        res = {'fatal': 'no result', 'cmds': {}}
    res['log'] = p.stdout
    if res.get('fatal'):
        raise RuntimeError('{}: {}\n{}'.format(tag, res['fatal'],
                                               p.stdout[-3000:]))
    return res


def stacks(res):
    r = res['cmds']['stacks']
    if r['err']:
        raise RuntimeError('dbg-stacks: {}\n{}'.format(r['err'],
                                                       res['log'][-3000:]))
    return json.loads(r['out'])


def label(t):
    return '{} {}'.format(t['name'] or '?', t['lwp'])


def lost(live, core, exact):
    '''The live threads whose stack the core does not hold.'''
    got = dict((t['lwp'], t) for t in core['threads'])
    out = []
    for t in live['threads']:
        c = got.get(t['lwp'])
        if c is None or c['mem'] is None:
            ok = False
        elif exact:
            ok = c['mem'] == t['mem'] and c['frames'] == t['frames']
        else:
            ok = c['mem'].strip('0') != '' and len(c['frames']) > 2 and \
                c['frames'][-1] == t['frames'][-1]
        if not ok:
            out.append(t)
    return out


class TestThreadStacks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.wd = tempfile.mkdtemp(prefix='dbg-cores-')
        script = os.path.join(cls.wd, 'script.lua')
        with open(script, 'w') as fh:
            fh.write(SCRIPT)
        # gdb's cores in a directory of their own: no core_pattern glob
        # of the kernel's core matches them.
        gdir = os.path.join(cls.wd, 'gdb')
        os.mkdir(gdir)
        paths = {'gcore': os.path.join(gdir, 'gcore'),
                 'pages': os.path.join(gdir, 'gcore-pages')}
        pattern, cls.no_kernel_core = kernel_core()
        cmds = [['stacks', 'dbg-stacks live'],
                ['gcore', 'gcore ' + paths['gcore']],
                ['pages', 'gcore-pages ' + paths['pages']]]
        if pattern:
            # SIGQUIT is not tarantool's: the kernel writes a core.  To
            # the main thread: the others block every signal.
            cmds += [['thread', 'thread 1'], ['kernel', 'signal SIGQUIT']]
        plan = {'binary': H.TARANTOOL, 'args': [script], 'cwd': cls.wd,
                'env': {}, 'sources': [STACKS, GCORE_PAGES],
                'breaks': ['break lj_cf_print'], 'setup': [], 'prep': [],
                'cmds': cmds}
        res = run_gdb(cls.wd, plan, 'live',
                      preexec=raise_core_limit if pattern else None)
        cls.live = stacks(res)
        cls.pages_out = res['cmds']['pages']['out'] + \
            (res['cmds']['pages']['err'] or '')
        if pattern:
            found = glob.glob(core_glob(pattern, cls.live['pid'], cls.wd))
            if found:
                paths['kernel'] = found[0]
            else:
                cls.no_kernel_core = 'no core {} was written: {}'.format(
                    core_glob(pattern, cls.live['pid'], cls.wd),
                    res['cmds']['kernel'])
        cls.cores = {}
        for kind, path in sorted(paths.items()):
            if os.path.exists(path):
                cls.cores[kind] = stacks(run_gdb(cls.wd, {
                    'binary': H.TARANTOOL, 'core': path, 'sources': [STACKS],
                    'cmds': [['stacks', 'dbg-stacks']]}, kind))
                if H.KEEP != 'cores':
                    os.unlink(path)
        cls.gdb = cls.live['gdb']
        main = cls.live['pid']
        cls.others = [t for t in cls.live['threads'] if t['lwp'] != main]
        cls.guarded = [t for t in cls.others if t.get('unreadable')]
        cls.known = None
        if cls.guarded and version(cls.gdb) < GDB_FIX:
            cls.known = (
                'gcore of gdb {} loses the stacks of threads that start '
                'with unreadable pages (guard pages of glibc {} installed '
                'with MADV_GUARD_INSTALL, Linux {}): {}'.format(
                    cls.gdb, GLIBC, KERNEL, BUG))
            # Decided here, after the live run: run() of a test reads the
            # flag that expectedFailure sets, after setUpClass.
            unittest.expectedFailure(cls.test_gcore)
            sys.stderr.write('\nknown-broken: {}\n'.format(cls.known))
        cls.write_summary()

    @classmethod
    def tearDownClass(cls):
        if not H.KEEP:
            shutil.rmtree(cls.wd, ignore_errors=True)

    @classmethod
    def verdict(cls, kind, exact=True):
        if kind not in cls.cores:
            return None
        return lost(cls.live, cls.cores[kind], exact)

    @classmethod
    def write_summary(cls):
        def text(kind, exact=True):
            v = cls.verdict(kind, exact)
            if v is None:
                return 'not taken: ' + (cls.no_kernel_core or 'no core')
            n = len(cls.live['threads'])
            return 'kept' if not v else 'lost {} of {}'.format(len(v), n)
        facts = {
            'os': os_name(), 'arch': os.uname().machine, 'kernel': KERNEL,
            'glibc': GLIBC, 'gdb': cls.gdb, 'tarantool': H.VERSION,
            'guard_pages': 'yes ({} of {} threads)'.format(
                len(cls.guarded), len(cls.others)) if cls.guarded else 'no',
            'gcore': text('gcore'), 'gcore_pages': text('pages'),
            'kernel_core': text('kernel', exact=False),
            'known': cls.known,
        }
        sys.stderr.write('\ncores: {}\n'.format(json.dumps(facts)))
        if SUMMARY:
            with open(SUMMARY, 'w') as fh:
                json.dump(facts, fh, indent=1)

    def assertKept(self, kind, exact=True):
        bad = self.verdict(kind, exact)
        self.assertEqual([label(t) for t in bad], [],
                         '{}: the core lost these stacks; the core has: {}'
                         .format(kind, json.dumps(
                             [(t['lwp'], t['frames'][:3]) for t in
                              self.cores[kind]['threads']])))

    def test_guard_pages(self):
        self.assertGreater(len(self.others), 2)
        main = [t for t in self.live['threads']
                if t['lwp'] == self.live['pid']][0]
        self.assertEqual(main.get('unreadable'), [], 'the main stack')
        want = [label(t) for t in self.others] if GUARD_IN_STACK else []
        self.assertEqual(
            [label(t) for t in self.guarded], want,
            'threads whose stack mapping has unreadable pages, with glibc '
            '{} and Linux {}: {}'.format(GLIBC, KERNEL, json.dumps(
                [(label(t), t['vma'], t.get('unreadable'))
                 for t in self.others])))

    def test_gcore(self):
        # An expected failure where setUpClass says why (self.known).
        self.assertKept('gcore')

    def test_gcore_pages(self):
        self.assertKept('pages')
        if self.guarded and version(self.gdb) < GDB_FIX:
            self.assertIn('gcore-pages: read again', self.pages_out)
        elif version(self.gdb) < GDB_FIX:
            self.assertIn('gcore-pages: gcore read every mapping',
                          self.pages_out)

    def test_kernel_core(self):
        if 'kernel' not in self.cores:
            self.skipTest(self.no_kernel_core)
        self.assertKept('kernel', exact=False)


if __name__ == '__main__':
    unittest.main()
