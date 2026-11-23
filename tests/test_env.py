# Layer env: how lj-bt meets its surroundings.  The LuaJIT extension
# missing names or not set up yet, a binary stripped of DWARF (alone and
# with `symbol-file` of the original, as README promises), the walk
# limits, examples/example.gdb, a LuaJIT extension of another release.

import json
import os
import re
import shutil
import subprocess
import unittest

import common as C

# tail.lua: box_index_get armed by the marker, as in test_regress.
STOPS = '''\
break box_index_get
commands
silent
{cmd}
continue
end
disable 1
break lbox_session_id
commands
silent
enable 1
continue
end
run
'''


def scenario(name='tail.lua'):
    return [os.path.join(C.LUA, name)]


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.wd = C.WorkDir('env')

    def tearDown(self):
        self.wd.cleanup()

    def gdb(self, script, args=None, core=None, binary=None, timeout=230):
        cmd = self.wd.write('env.gdb', script)
        argv = ['gdb', '--batch', '--nx', '--quiet', '--command', cmd]
        if core:
            argv += [binary or C.TARANTOOL, core]
        elif args is not None:
            argv += ['--args', binary or C.TARANTOOL] + args
        env = dict(os.environ, TMPDIR=self.wd.path,
                   LJBT_WORKDIR=self.wd.path, LJBT_LUA_DIR=C.LUA,
                   LJBT_JIT_MODE='off')
        p = subprocess.run(argv, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           universal_newlines=True, timeout=timeout, env=env,
                           cwd=C.ROOT)
        return p.stdout

    def test_missing_names(self):
        fake = self.wd.write('fake.py', 'mref = gcref = gcval = funcproto = '
                             'typenames = itypemap = None\n')
        out = self.gdb('source {}\nsource {}\n'.format(fake, C.LJ_BT))
        self.assertRegex(out, r'missing: G, J, frame_ftsz, vm_state, '
                              r'LJ_GC64, LJ_FR2')
        self.assertNotIn('lj-bt command initialized', out)

    def test_stack_bottoms(self):
        # How the walk of the C stack ends, on fake frames: the bottom of
        # a fiber that has not run yet (coro_create's context: pc 0 below
        # coro_transfer, "previous frame identical" on arm64, as fiber 103
        # "on_shutdown" of every Tarantool) is a "--" note, as is a named
        # bottom; pc 0 anywhere else and a nameless end stay "!!".
        cases = {
            'fresh_arm64': [('fiber_yield', 0x10), ('coro_transfer', 0x20),
                            (None, 0, 'SAME_ID')],
            'fresh_amd64': [('coro_init', 0x20), (None, 0, 'OUTERMOST')],
            'pc0_elsewhere': [('fiber_yield', 0x10), ('memcpy', 0x20),
                              (None, 0, 'SAME_ID')],
            'named_bottom': [('fiber_loop', 0x10),
                             ('coro_startup', 0x20, 'SAME_ID')],
            'garbage': [('fiber_yield', 0x10), (None, 0x30, 'SAME_ID')],
        }
        script = self.wd.write('bottoms.py', '''\
import json
import gdb


class Fake(object):
    def __init__(self, frames, i):
        self.frames, self.i = frames, i

    def level(self):
        return self.i

    def name(self):
        return self.frames[self.i][0]

    def pc(self):
        return self.frames[self.i][1]

    def older(self):
        if self.i + 1 < len(self.frames):
            return Fake(self.frames, self.i + 1)
        return None

    def unwind_stop_reason(self):
        f = self.frames[self.i]
        return getattr(gdb, 'FRAME_UNWIND_' + (f[2] if len(f) > 2 else
                                               'NO_REASON'))


res = {}
real = gdb.newest_frame, LJBTCFrame
LJBTCFrame = lambda f: f.name()
try:
    for k, frames in json.loads(%r).items():
        gdb.newest_frame = lambda: Fake(frames, 0)
        res[k] = ljbt_walk_cstack(False, 256)
finally:
    gdb.newest_frame, LJBTCFrame = real
print('BOTTOMS ' + json.dumps(res))
''' % json.dumps(cases))
        out = self.gdb('source {}\nsource {}\nsource {}\n'.format(
            C.LUAJIT_DBG, C.LJ_BT, script))
        m = re.search(r'(?m)^BOTTOMS (.*)$', out)
        self.assertIsNotNone(m, out)
        res = json.loads(m.group(1))
        notes = dict((k, v[1]) for k, v in res.items())
        self.assertEqual(len(notes['fresh_arm64']), 1, notes)
        self.assertRegex(notes['fresh_arm64'][0],
                         r'^-- .*pc 0 below coro_transfer.*has not run yet')
        self.assertEqual(notes['fresh_amd64'], [])
        self.assertRegex(notes['named_bottom'][0], r'^-- .*coro_startup')
        for k in ('pc0_elsewhere', 'garbage'):
            self.assertEqual(len(notes[k]), 1, notes)
            self.assertRegex(notes[k][0], r'^!! unwinding stopped: ', k)
        self.assertEqual(res['fresh_arm64'][0],
                         ['fiber_yield', 'coro_transfer', None])

    def test_not_initialized(self):
        # No binary: luajit_dbg.py postpones its setup, LJ_GC64 is None.
        out = self.gdb('source {}\nsource {}\nlj-bt\n'.format(
            C.LUAJIT_DBG, C.LJ_BT))
        self.assertIn('lj-bt: the LuaJIT extension is not initialized', out)
        self.assertNotIn('Traceback', out)

    def stripped(self):
        if not shutil.which('objcopy'):
            self.skipTest('no objcopy')
        path = self.wd.file('tarantool-stripped')
        subprocess.run(['objcopy', '--strip-debug', C.TARANTOOL, path],
                       check=True, timeout=120)
        return path

    def test_stripped_without_symbols(self):
        # No DWARF: one message, then plain bt; no traceback per bt.
        path = self.stripped()
        cmd = ('echo ==raw\\n\nbt -no-filters\necho ==bt1\\n\nbt\n'
               'echo ==bt2\\n\nbt\nkill')
        out = self.gdb(C.gdb_prelude() + STOPS.format(cmd=cmd),
                       scenario(), binary=path)
        self.assertLessEqual(out.count('lj-bt frame filter:'), 1, out)
        self.assertNotIn('Traceback', out)
        raw = out.split('==raw')[1].split('==bt1')[0]
        bt1 = out.split('==bt1')[1].split('==bt2')[0]
        frames = re.compile(r'(?m)^#\d+ .*$')
        self.assertEqual(frames.findall(bt1), frames.findall(raw))

    def test_stripped_with_symbol_file(self):
        # README: the deb binary of the same build serves as symbol file.
        path = self.stripped()
        out = self.gdb('symbol-file {}\n'.format(C.TARANTOOL) +
                       C.gdb_prelude() +
                       STOPS.format(cmd='lj-bt\nkill'), scenario(),
                       binary=path)
        self.assertRegex(out, r'(?m)^#\d+\.\d+\s+\[lua\] ')
        self.assertNotRegex(out, r'(?m)^\s*!!')

    def test_limits_marked(self):
        lim = self.wd.write('limits.py', 'LJBT_LIMITS.update(cframes=1, '
                            'links=3)\n')
        out = self.gdb(C.gdb_prelude() + 'source {}\n'.format(lim) +
                       STOPS.format(cmd='lj-bt\nkill'), scenario())
        self.assertIn('!! L=', out)
        self.assertRegex(out, r'more than 1 cframes|more than 3 frame links')

    def test_no_enum_in_dwarf(self):
        # tarantool/tarantool#13094: an enum used only through its
        # constants may be absent from DWARF.  lj-bt then takes the names
        # of the LuaJIT extension, noted, or refuses clearly.  The refusal
        # is quick too: a run takes a second, the trace unwinder must not
        # try the failing build again on each of the thousands of times
        # gdb asks it before the stop (it took the run past any timeout).
        hide = self.wd.write('noenum.py', (
            'import gdb\n'
            '_orig = ljbt_enum\n'
            'def ljbt_enum(example):\n'
            '    if example in ("BC_CALL", "IR_KGC"):\n'
            '        raise gdb.error("No symbol \\"%s\\" in current context."'
            ' % example)\n'
            '    return _orig(example)\n'))
        notab = self.wd.write('notab.py', 'BYTECODES = IRS = None\n')
        for extra, expect in ((hide, r'(?m)^-- bytecode names from the '
                                     r'BYTECODES table'),
                              (hide + '\nsource ' + notab,
                               r'lj-bt: no bytecode names: DWARF has no '
                               r'enum with BC_CALL')):
            with self.subTest(extra=os.path.basename(extra)):
                out = self.gdb(C.gdb_prelude() + 'source {}\n'.format(
                    extra) + STOPS.format(cmd='lj-bt\nkill'), scenario(),
                    timeout=60)
                if 'BYTECODES' in expect and \
                        'luajit-gdb.py' in C.LUAJIT_DBG:
                    continue        # The old extension has no tables.
                self.assertRegex(out, expect)
                self.assertNotIn('Traceback', out)
                if 'BYTECODES' in expect:
                    # The names serve: Lua frames, the tail call heuristic
                    # (it decodes the CALL), no mark.
                    self.assertRegex(out, r'(?m)^#\d+\.\d+\s+\[lua\] ')
                    self.assertIn('[heuristic]', out)
                    self.assertNotRegex(out, r'(?m)^\s*!!')

    def test_example(self):
        # examples/example.gdb as README says to run it, from the root.
        with open(os.path.join(C.ROOT, 'examples', 'example.gdb')) as fh:
            script = fh.read()
        out = self.gdb(script, ['examples/mixed.lua', 'sort', 'on', 'main'])
        self.assertNotIn('Traceback', out)
        self.assertNotIn('Python Exception', out)
        self.assertRegex(out, r'(?m)^#\d+\.\d+\s+\[lua\] ')
        self.assertRegex(out, r'tail call from method .get.')

    @unittest.skipUnless(os.environ.get('LJBT_CROSS_LUAJIT_DBG'),
                         'set LJBT_CROSS_LUAJIT_DBG to the LuaJIT extension '
                         'of another release')
    def test_cross_extension(self):
        # lj-bt reads bytecode and IR names from DWARF and takes only the
        # helpers both extensions have: the extension of another release
        # works as long as the structures did not change (recorded fact,
        # 3.5 vs 3.9 both ways).
        pre = C.gdb_prelude().replace(C.LUAJIT_DBG,
                                      os.environ['LJBT_CROSS_LUAJIT_DBG'])
        out = self.gdb(pre + STOPS.format(cmd='lj-bt\nkill'), scenario())
        self.assertRegex(out, r'(?m)^#\d+\.\d+\s+\[lua\] ')
        self.assertNotRegex(out, r'(?m)^\s*!!')


if __name__ == '__main__':
    unittest.main()
