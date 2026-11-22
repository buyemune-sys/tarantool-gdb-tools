# Layer env: how lj-bt meets its surroundings.  The LuaJIT extension
# missing names or not set up yet, a binary stripped of DWARF (alone and
# with `symbol-file` of the original, as README promises), the walk
# limits, examples/example.gdb, a LuaJIT extension of another release.

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

    def gdb(self, script, args=None, core=None, binary=None):
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
                           universal_newlines=True, timeout=230, env=env,
                           cwd=C.ROOT)
        return p.stdout

    def test_missing_names(self):
        fake = self.wd.write('fake.py', 'mref = gcref = gcval = funcproto = '
                             'typenames = itypemap = None\n')
        out = self.gdb('source {}\nsource {}\n'.format(fake, C.LJ_BT))
        self.assertRegex(out, r'missing: G, J, frame_ftsz, vm_state, '
                              r'LJ_GC64, LJ_FR2')
        self.assertNotIn('lj-bt command initialized', out)

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
