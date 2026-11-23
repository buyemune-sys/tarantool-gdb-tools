# Tests of tests/manifest/check.py: extensions.json passes, and a wrong
# manifest (or a workflow that no longer covers it) fails, each wrong
# field with its own error.  The tests that load the files into gdb need
# $LJBT_TARANTOOL and gdb, and are skipped without them.
#
#   cd tests/manifest && python3 -m unittest -v test_check
import copy
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check  # noqa: E402

MAN = check.load(check.MANIFEST)


def ext(man, name):
    return check.by_file(man)[name]


def wrong(fn):
    '''A copy of the manifest changed by fn.'''
    man = copy.deepcopy(MAN)
    fn(man)
    return man


class Case(unittest.TestCase):
    def assertCaught(self, errs, text):
        self.assertTrue(any(text in e for e in errs),
                        'no error with {!r} in {!r}'.format(text, errs))


class TestStatic(Case):
    def test_manifest(self):
        self.assertEqual(check.static_errors(MAN), [])

    def test_wrong_manifest(self):
        lj, tt, bt, gp = ('luajit_gdb.py', 'tarantool_gdb.py',
                          'luajit_backtrace_gdb.py', 'gcore_pages_gdb.py')
        cases = [
            ('schema', lambda m: m.update(schema=2), 'only 1 is known'),
            ('unknown key', lambda m: ext(m, tt).update(command=[]),
             'unknown key command'),
            ('no commands', lambda m: ext(m, tt).pop('commands'),
             'no commands'),
            ('twice', lambda m: m['extensions'].append(ext(m, gp)),
             'listed twice'),
            ('debugger', lambda m: ext(m, tt)['debuggers'].append('windbg'),
             'unknown debugger windbg'),
            ('arch', lambda m: ext(m, tt)['arch'].append('riscv64'),
             'unknown arch riscv64'),
            ('range', lambda m: ext(m, tt)['tarantool'].update(min='3.9.1'),
             'min is above max'),
            ('file', lambda m: ext(m, gp).update(file='gcore_pages.py'),
             'gcore_pages.py: no such file'),
            ('after unknown', lambda m: ext(m, bt)['after'].append('lj_bt.py'),
             'lj_bt.py is not in the manifest'),
            ('after cycle', lambda m: ext(m, lj)['after'].append(bt),
             'a cycle through'),
            ('after self', lambda m: ext(m, lj)['after'].append(lj),
             'a cycle through luajit_gdb.py'),
            ('after debugger', lambda m: ext(m, lj)['after'].append(tt),
             'after tarantool_gdb.py, which has no lldb'),
            ('after range', lambda m: ext(m, bt)['tarantool'].update(
                min='3.0.2', max='3.9.0') or ext(m, lj)['tarantool'].update(
                min='3.3.5'), 'whose tarantool range is narrower'),
            ('no CI for the debugger',
             lambda m: ext(m, tt)['debuggers'].append('lldb'),
             'tarantool_gdb.py in lldb: no workflow checks it'),
            ('max beyond CI',
             lambda m: ext(m, lj)['tarantool'].update(max='3.10.0'),
             'tarantool.max 3.10.0 is not in the matrix of dbg.yml'),
            ('min below CI',
             lambda m: ext(m, bt)['tarantool'].update(min='3.2.4'),
             'tarantool.min 3.2.4 is not in the matrix of test.yml'),
        ]
        for what, fn, text in cases:
            with self.subTest(what):
                self.assertCaught(check.static_errors(wrong(fn)), text)


class TestWorkflows(Case):
    '''The manifest as it is, the workflows changed.'''

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='manifest-wf-')
        self.orig = check.WORKFLOWS
        check.WORKFLOWS = os.path.join(self.tmp, 'workflows')
        shutil.copytree(self.orig, check.WORKFLOWS)

    def tearDown(self):
        check.WORKFLOWS = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, name, old, new):
        path = os.path.join(check.WORKFLOWS, name)
        with open(path) as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(path, 'w') as fh:
            fh.write(text.replace(old, new))

    def test_copy(self):
        self.assertEqual(check.static_errors(MAN), [])

    def test_minor_gone(self):
        self.edit('dbg.yml', '"3.4.2", ', '')
        errs = check.static_errors(MAN)
        self.assertCaught(errs, 'luajit_gdb.py in gdb: no 3.4 in the matrix '
                                'of dbg.yml')
        self.assertCaught(errs, 'tarantool_gdb.py in gdb: no 3.4')
        self.assertCaught(errs, 'gcore_pages_gdb.py in gdb: no 3.4')

    def test_arch_gone(self):
        self.edit('lldb.yml', "'[\"amd64\", \"arm64\"]'", "'[\"amd64\"]'")
        self.assertCaught(check.static_errors(MAN),
                          'luajit_gdb.py in lldb: no aarch64 in the matrix '
                          'of lldb.yml')

    def test_check_not_run(self):
        self.edit('test.yml', 'tests/manifest/check.py gdb', 'true')
        self.assertCaught(check.static_errors(MAN),
                          'luajit_backtrace_gdb.py in gdb: test.yml does not '
                          'run tests/manifest/check.py gdb')


def fake_load(name, commands, parameters=(), error=None):
    return {'file': name, 'commands': list(commands),
            'parameters': list(parameters), 'output': '', 'error': error}


class TestCompare(Case):
    '''compare() on what a debugger would have said: the manifest
    itself, then something else.'''

    def real(self, man=MAN):
        return {e['file']: {'deps': [], 'self': fake_load(
            e['file'], e['commands'], e.get('parameters', []))}
            for e in man['extensions'] if 'gdb' in e['debuggers']}

    def test_same(self):
        self.assertEqual(check.compare(MAN, 'gdb', self.real()), [])

    def test_differs(self):
        real = self.real()
        real['tarantool_gdb.py']['self']['commands'].append('tt-tuple')
        real['tarantool_gdb.py']['self']['parameters'].remove(
            'tl-var-search-range')
        real['luajit_gdb.py']['self']['commands'].remove('lj-gctop')
        real['luajit_backtrace_gdb.py']['deps'] = [fake_load(
            'luajit_gdb.py', [], error='boom')]
        real['gcore_pages_gdb.py']['self']['error'] = 'bang'
        errs = check.compare(MAN, 'gdb', real)
        self.assertCaught(errs, 'tarantool_gdb.py in gdb: registers commands '
                                'the manifest has not: tt-tuple')
        self.assertCaught(errs, 'tarantool_gdb.py in gdb: does not register '
                                'parameters of the manifest: '
                                'tl-var-search-range')
        self.assertCaught(errs, 'luajit_gdb.py in gdb: does not register '
                                'commands of the manifest: lj-gctop')
        self.assertCaught(errs, 'luajit_backtrace_gdb.py in gdb: loading '
                                'luajit_gdb.py (after) failed: boom')
        self.assertCaught(errs, 'gcore_pages_gdb.py in gdb: loading failed: '
                                'bang')
        self.assertEqual(len(errs), 5, errs)


@unittest.skipUnless(os.environ.get('LJBT_TARANTOOL') and
                     shutil.which(os.environ.get('DBG_GDB', 'gdb')),
                     'set LJBT_TARANTOOL (and have gdb) to load the files')
class TestRegistered(Case):
    '''What gdb says each file registers, against the manifest and
    against wrong ones.'''

    @classmethod
    def setUpClass(cls):
        cls.real = check.registered(MAN, 'gdb')

    def test_manifest(self):
        self.assertEqual(check.compare(MAN, 'gdb', self.real), [])

    def test_wrong_manifest(self):
        tt, bt = 'tarantool_gdb.py', 'luajit_backtrace_gdb.py'
        if self.real.get(bt) is None:
            bt = None                   # lj-bt is not for this binary
        cases = [
            ('a command less', lambda m: ext(m, tt)['commands'].remove(
                'tt-mp'), 'registers commands the manifest has not: tt-mp'),
            ('a command more', lambda m: ext(m, tt)['commands'].append(
                'tt-tuple'), 'does not register commands of the manifest: '
             'tt-tuple'),
            ('a parameter less', lambda m: ext(m, tt)['parameters'].remove(
                'print tt-list-entry'), 'registers parameters the manifest '
             'has not: print tt-list-entry'),
            ('a parameter as a command', lambda m: ext(m, tt).update(
                commands=ext(m, tt)['commands'] + ['tl-var-search-range'],
                parameters=['print tt-list-entry']),
             'does not register commands of the manifest: '
             'tl-var-search-range'),
        ]
        if bt:
            cases.append(('a renamed command', lambda m: ext(m, bt).update(
                commands=['lj-backtrace']), 'lj-backtrace'))
        for what, fn, text in cases:
            with self.subTest(what):
                self.assertCaught(check.compare(wrong(fn), 'gdb', self.real),
                                  text)

    def test_after_missing(self):
        # lj-bt without luajit_gdb.py before it does not load: the check
        # loads a file after its `after` only.
        bt = 'luajit_backtrace_gdb.py'
        if self.real.get(bt) is None:
            self.skipTest('lj-bt is not for this binary')
        man = wrong(lambda m: m.update(extensions=[ext(m, bt)]))
        ext(man, bt)['after'] = []
        errs = check.compare(man, 'gdb', check.registered(man, 'gdb'))
        self.assertCaught(errs, 'luajit_backtrace_gdb.py in gdb: loading '
                                'failed: luajit_backtrace_gdb.py: source '
                                'luajit_gdb.py')
        self.assertCaught(errs, 'does not register commands of the '
                                'manifest: lj-bt')


if __name__ == '__main__':
    unittest.main()
