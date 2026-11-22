# Layer cli: lj-bt driven through gdb commands as a user does (cli.lua,
# cli_gdb.py): every flag, wrong flags, repeated calls, the state of gdb
# around them, `thread N`, a suspended coroutine, more C frames than the
# -n limit, long local and upvalue names, a VM math helper, the VM
# returning to C, a stop in the iproto thread, `tt-fiber` then `stepi`.

import json
import os
import re
import subprocess
import unittest

import common as C

SCRIPT = '''\
set backtrace limit 3000
break lbox_session_exists if $ljbt_arg(L, 0) >= 5000
source {cli}
ljbt-cli-run {out} {tt}
'''


def marks(text):
    return [line for line in (text or '').splitlines()
            if line.strip().startswith('!!')]


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = C.WorkDir('cli')
        out = cls.wd.file('cli.jsonl')
        C.run_gdb(cls.wd, C.gdb_prelude() + SCRIPT.format(
            cli=C.CLI_GDB, out=out, tt=C.TT_GDB or ''),
            [os.path.join(C.LUA, 'cli.lua')])
        cls.recs = C.read_jsonl(out)
        cls.out = out
        p = subprocess.run(['gdb', '--batch', '--nx', '-ex',
                            'info address tarantool_L', C.TARANTOOL],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=120)
        cls.tls_L = 'thread-local' in p.stdout

    @classmethod
    def tearDownClass(cls):
        cls.wd.cleanup()

    def get(self, n, what):
        return [r for r in self.recs if r['n'] == n and r['what'] == what]

    def one(self, n, what):
        r = self.get(n, what)
        self.assertEqual(len(r), 1, '{} {}: {}'.format(n, what, r))
        return r[0]

    def test_no_exception(self):
        self.assertEqual([r['error'] for r in self.recs
                          if r['what'] == 'exception'], [])

    def test_flags(self):
        flags = dict((r['args'], r) for r in self.get(1, 'flag'))
        for args, r in flags.items():
            self.assertIsNone(r['error'], args)
        self.assertIn('[L link 0x', flags['-v']['text'])
        self.assertRegex(flags['-f']['text'], r' at /\S+:\d+')
        self.assertNotRegex(flags['']['text'], r' at /\S+:\d+')
        cframes = re.findall(r'(?m)^#\d+ ', flags['-n 3']['text'])
        self.assertEqual(len(cframes), 3)
        self.assertIn('-- more frames follow', flags['-n 3']['text'])
        self.assertIn('tail call from', flags['']['text'])
        self.assertNotIn('tail call from', flags['--no-tail']['text'])
        doc = json.loads(flags['--json']['text'])
        self.assertEqual(doc['header'], flags['']['text'].splitlines()[0])

    def test_output_file(self):
        r = self.one(1, 'flag-o')
        with open(r['path']) as fh:
            text = fh.read()
        once = self.get(1, 'flag')[0]['text']
        self.assertEqual(text, once + once)

    def test_bad_flags(self):
        for r in self.get(1, 'bad-flag'):
            self.assertIsNotNone(r['error'], r['args'])
            self.assertTrue(r['error'].startswith('lj-bt: '), r['error'])

    def test_idempotent(self):
        docs = [json.loads(d)['frames'] for d in
                self.one(1, 'idempotent')['docs']]
        self.assertEqual(docs[0], docs[1])
        self.assertEqual(docs[0], docs[2])
        r = self.one(1, 'state')
        self.assertEqual(r['before'], r['after'])

    def check_consistent(self, r):
        '''No "!!", the filtered bt shows the same Lua lines as lj-bt, the
        text agrees with the JSON.'''
        self.assertIsNone(r['error'])
        self.assertEqual(marks(r['text']), [])
        self.assertEqual(r['bt_lua'], r['text_lua'])
        doc = r['doc']
        self.assertEqual(len(marks(r['text'])), len(C.warnings(doc)))
        numbered = re.findall(r'(?m)^#\d+\.\d+ ', r['text'])
        entries = [e for f in doc['frames'] for e in f['lua']]
        self.assertEqual(len(numbered), len(entries))

    def test_stop_consistent(self):
        for n in (1, 4, 5):
            with self.subTest(n=n):
                self.check_consistent(self.one(n, 'stop'))

    def no_lua_here(self, r):
        # A thread with no Lua: nothing stitched into its C stack.  Where
        # tarantool_L is __thread (3.7+) its G is NULL there, no Lua at
        # all; where it is a plain global (3.3-3.6) the tx Lua stack may
        # follow, under the header saying it is not on this C stack.
        self.assertEqual([s for s in r['text'].splitlines()
                          if re.match(r'#\d+\.\d+\s+\[lua\]', s)], [])
        if self.tls_L:
            self.assertEqual(r['text_lua'], [])
        elif r['text_lua']:
            self.assertIn('-- no Lua runs on this C stack', r['text'])

    def test_threads(self):
        ths = self.get(1, 'thread')
        ids = self.get(1, 'thread-id')
        self.assertGreater(len(ths), 1)
        for r, i in zip(ths, ids):
            with self.subTest(thread=i['num'], name=i['name']):
                self.assertEqual(marks(r['text']), [])
                self.assertEqual(r['bt_lua'], r['text_lua'])
                if i['num'] == 1:
                    self.assertTrue(r['text_lua'])
                else:
                    self.no_lua_here(r)

    def test_suspended_coroutine(self):
        r = self.one(2, 'coroutine')
        self.assertEqual(marks(r['text']), [])
        with open(os.path.join(C.LUA, 'cli.lua')) as fh:
            line = next(i + 1 for i, s in enumerate(fh.read().splitlines())
                        if 'CO_YIELD' in s)
        lines = [e.get('currentline') for e in r['doc']['tail']]
        self.assertIn(line, lines, r['text'])

    def test_deep(self):
        r = self.one(3, 'deep')
        self.assertEqual(marks(r['text']), [])
        self.assertIn('-- more frames follow', r['text'])
        r = self.one(3, 'deep-all')
        self.assertEqual(marks(r['text']), [])
        self.assertGreater(len(r['doc']['frames']), 256)
        nest = [e for f in r['doc']['frames'] for e in f['lua']
                if e.get('chunk', '') and e['chunk'].endswith('cli.lua')]
        self.assertGreaterEqual(len(nest), 90)

    def test_long_names(self):
        def called(r):
            return [e['called'] for f in r['doc']['frames'] for e in f['lua']
                    if e['kind'] == 'heuristic']
        self.assertIn("local 'f'", called(self.one(4, 'stop')))
        self.assertIn("upvalue 'uv_last'", called(self.one(5, 'stop')))

    def test_vm_math_helper(self):
        r = self.get(0, 'floor')
        if not r or r[0].get('doc') is None:
            self.skipTest('no lj_vm_floor call (arm64 rounds inline)')
        r = r[0]
        self.assertEqual(r['doc']['frames'][0]['kind'], 'c')
        self.assertEqual(marks(r['text']), [])

    def test_vm_top_noted(self):
        # Stopped inside the VM where its registers say nothing: the top
        # comes from L->base, which may be behind; a note says so.
        r = self.one(0, 'ff')
        self.assertTrue(any('stopped in the VM' in n
                            for n in r['doc']['notes']), r['text'])
        self.assertEqual(marks(r['text']), [])

    def test_vm_return_marked(self):
        r = self.one(0, 'vm_return')
        notes = r['doc']['notes'] + [w for f in r['doc']['frames']
                                     for w in f['warnings']]
        self.assertTrue(any('stopped in the VM' in n or n.startswith('!!')
                            for n in notes), r['text'])

    def test_iproto_thread(self):
        r = self.one(0, 'iproto')
        self.assertEqual(marks(r['text']), [])
        self.no_lua_here(r)
        self.assertEqual(r['bt_lua'], r['text_lua'])
        for t in self.get(0, 'thread'):
            self.assertEqual(marks(t['text']), [])
            self.assertEqual(t['bt_lua'], t['text_lua'])

    @unittest.skipUnless(C.TT_GDB, 'LJBT_TT_GDB is not set')
    def test_sleeper_fiber(self):
        a = self.one(9, 'sleeper')
        b = self.one(9, 'sleeper-stepi')
        for r in (a, b):
            self.assertIn('cli-sleeper', r['doc']['header'])
            self.assertIn('(not running)', r['doc']['header'])
            self.assertEqual(marks(r['text']), [])
            self.assertEqual(r['bt_lua'], r['text_lua'])
        allf = self.one(9, 'all-fibers')
        mine = [d for d in allf['docs']
                if d['fiber'].startswith('fiber {} '.format(allf['fid']))]
        self.assertEqual(len(mine), 1)
        self.assertEqual(
            [e['text'] for e in C.entries(mine[0]['bt'])],
            [e['text'] for e in C.entries(a['doc'])])
        for d in allf['docs']:
            if d['bt'] is not None:
                self.assertEqual(C.warnings(d['bt']), [], d['fiber'])


if __name__ == '__main__':
    unittest.main()
