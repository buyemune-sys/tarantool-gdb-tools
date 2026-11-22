# Shared helpers of the lj-bt tests.
#
# Environment:
#   LJBT_TARANTOOL   tarantool binary with DWARF (required)
#   LJBT_LUAJIT_DBG  luajit_dbg.py of the same build (required)
#   LJBT_TT_GDB      tools/tarantool-gdb.py (optional: fiber tests)
#   LJBT_SEEDS       layer 2: number of random chains (default 200)
#   LJBT_SEED0       layer 2: first seed (default 1)
#   LJBT_STOPS       layer 3: number of random stops (default 500)
#   LJBT_JIT         on, off or both (default both)
#   LJBT_ATTEMPTS    layer 5: runs per sysprof crash (default 3)
#   LJBT_KEEP        keep work directories (default: removed)
#   LJBT_VERBOSE     print gdb commands and output
#   TMPDIR           where work directories go (tempfile)

import json
import os
import re
import shutil
import subprocess
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
LUA = os.path.join(TESTS, 'lua')
# LJBT_LJ_BT runs the tests against another lj_bt.py, e.g. an older one,
# to see a new test fail before its fix.
LJ_BT = os.environ.get('LJBT_LJ_BT') or os.path.join(ROOT, 'lj_bt.py')
PROBE = os.path.join(TESTS, 'probe.py')

TARANTOOL = os.environ.get('LJBT_TARANTOOL')
LUAJIT_DBG = os.environ.get('LJBT_LUAJIT_DBG')
TT_GDB = os.environ.get('LJBT_TT_GDB')
VERBOSE = bool(os.environ.get('LJBT_VERBOSE'))
KEEP = bool(os.environ.get('LJBT_KEEP'))

if not TARANTOOL or not LUAJIT_DBG:
    raise RuntimeError('set LJBT_TARANTOOL and LJBT_LUAJIT_DBG')


def env_int(name, default):
    return int(os.environ.get(name, default))


def jit_modes():
    m = os.environ.get('LJBT_JIT', 'both')
    return ['on', 'off'] if m == 'both' else [m]


def lua_eval(code, timeout=60):
    '''Run a Lua snippet with the tarantool binary, return its stdout.'''
    p = subprocess.run([TARANTOOL, '-e', code + '; os.exit(0)'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, universal_newlines=True,
                       timeout=timeout)
    return p.stdout.strip()


IS_GC64 = lua_eval("print(require('ffi').abi('gc64'))") == 'true'

# sysprof drives layer 3 and the crashes of layer 5; LuaJIT has no
# sysprof on arm64 (nor memprof), start() fails there.
HAS_SYSPROF = lua_eval(
    "local ok = misc.sysprof.start({mode = 'D', interval = 100}) "
    "if ok then misc.sysprof.stop() end print(ok == true)") == 'true'
NO_SYSPROF = 'no sysprof in this build (LuaJIT has none on arm64)'


class WorkDir(object):
    '''A temporary directory for one tarantool run (box files, unix
    socket, gdb scripts, probe output).  Short path: unix sockets.'''

    def __init__(self, name):
        self.path = tempfile.mkdtemp(prefix='ljbt-{}-'.format(name))

    def file(self, name):
        return os.path.join(self.path, name)

    def write(self, name, text):
        p = self.file(name)
        with open(p, 'w') as fh:
            fh.write(text)
        return p

    def cleanup(self):
        if not KEEP:
            shutil.rmtree(self.path, ignore_errors=True)


def gdb_prelude(tt_gdb_after_start=False):
    '''Common gdb settings and the extensions.  tarantool-gdb.py reads
    TLS on load, so it is sourced only after the process has started.'''
    lines = [
        'set pagination off',
        'set confirm off',
        'set print pretty off',
        'set print frame-arguments none',
        'set width 0',
        # A garbage stack (random stops) must not unwind forever.
        'set backtrace limit 300',
        'handle SIGPIPE nostop noprint pass',
        'handle SIGUSR1 nostop noprint pass',
        'source ' + LUAJIT_DBG,
        'source ' + LJ_BT,
        'source ' + PROBE,
    ]
    # Debugging aid / mutation testing: one more file after lj_bt.py.
    if os.environ.get('LJBT_EXTRA_SOURCE'):
        lines.append('source ' + os.environ['LJBT_EXTRA_SOURCE'])
    return '\n'.join(lines) + '\n'


def tt_gdb_source():
    if not TT_GDB:
        return ''
    return 'python import gdb.printing\nsource {}\n'.format(TT_GDB)


def run_gdb(wd, script, args, timeout=230, core=None):
    '''Run gdb with the script on tarantool <args> (or on a core file).
    Returns (returncode, output).'''
    cmd = wd.write('cmds.gdb' if core is None else 'core.gdb', script)
    argv = ['gdb', '--batch', '--nx', '--quiet', '--command', cmd]
    if core is None:
        argv += ['--args', TARANTOOL] + list(args)
    else:
        argv += [TARANTOOL, core]
    env = dict(os.environ)
    env['TMPDIR'] = wd.path
    env['LJBT_WORKDIR'] = wd.path
    env['LJBT_LUA_DIR'] = LUA
    if VERBOSE:
        print('# gdb: ' + ' '.join(argv))
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           universal_newlines=True, timeout=timeout, env=env,
                           cwd=wd.path)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ''
        if isinstance(out, bytes):
            out = out.decode('utf-8', 'replace')
        out, rc = out + '\n<timeout>\n', -1
    if VERBOSE:
        print(out)
    with open(wd.file('gdb.log'), 'a') as fh:
        fh.write(out)
    return rc, out


# $ljbt_arg (probe.py) reads the Lua number in both number modes: a plain
# `->n` misses every integer of the dual-number arm64 build.
BREAK_TARGETS = '''\
break lbox_session_exists if $ljbt_arg(L, 0) >= 4000
commands
silent
{tt}ljbt-probe {probe} {opts}
continue
end
break box_space_id_by_name if len >= 40
commands
silent
{tt}ljbt-probe {probe} {opts}
continue
end
'''


def run_chains(wd, program, probe_opts='', timeout=230):
    '''Run a generated program under gdb, stopping at every target call.
    Returns (stops by seq, oracle levels by seq, failures, gdb output).'''
    src = wd.write('chains.lua', program.render())
    probe = wd.file('probe.jsonl')
    tt = 'ljbt-probe-tt {}\n'.format(TT_GDB) if TT_GDB else ''
    script = gdb_prelude() + BREAK_TARGETS.format(
        probe=probe, opts=probe_opts, tt=tt) + 'run\n'
    rc, out = run_gdb(wd, script, [src], timeout=timeout)
    stops = {}
    for rec in read_jsonl(probe):
        stops.setdefault(rec['seq'], []).append(rec)
    oracle = dict((r['seq'], r['levels'])
                  for r in read_jsonl(wd.file('oracle.jsonl')))
    failures = read_jsonl(wd.file('failures.jsonl'))
    return stops, oracle, failures, out


def read_jsonl(path):
    res = []
    if not os.path.exists(path):
        return res
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                res.append(json.loads(line))
    return res


# lj-bt --json documents. ########################################


def entries(doc, L=None):
    '''Lua-side entries of a document in output order (newest first):
    folded C functions, Lua frames and heuristic lines.'''
    res = []
    for f in doc['frames']:
        if f.get('folded'):
            res.append(dict(f['folded'], cframe_level=f['level']))
        for e in f['lua']:
            res.append(dict(e, cframe_level=f['level']))
    if L is not None:
        res = [e for e in res if e.get('L') == L or
               (e['kind'] == 'heuristic' and L == '*')]
    return res


def warnings(doc):
    '''All "!!" marks of a document.'''
    w = [n for n in doc['notes'] if n.startswith('!!')]
    for f in doc['frames']:
        w += f['warnings']
    w += [e['text'] for e in doc['tail'] if e.get('kind') == 'warn']
    w += ['!! unmatched: ' + e['text'] for e in doc['tail']
          if e.get('kind') != 'warn']
    return w


def cframes_mismatch(doc, bt):
    '''None if the C frames of lj-bt are exactly those of bt -no-filters
    (same levels, same functions, same order), else a description.'''
    mine = [(f['level'], norm_name(f)) for f in doc['frames']]
    theirs = [(b['level'], b['name']) for b in bt]
    if mine == theirs:
        return None
    n = min(len(mine), len(theirs))
    for i in range(n):
        if mine[i] != theirs[i]:
            return 'frame #{}: lj-bt {} vs bt {}'.format(i, mine[i],
                                                         theirs[i])
    return 'lj-bt has {} frames, bt has {}'.format(len(mine), len(theirs))


def is_subsequence(small, big):
    it = iter(big)
    return all(any(s == b for b in it) for s in small)


def check_doc(rec, allow_warnings=False):
    '''Invariants of one stop that need no model.  Returns problems.'''
    probs = []
    if rec.get('error'):
        return ['python exception in lj-bt:\n' + rec['error']]
    doc = rec['ljbt']
    m = cframes_mismatch(doc, rec['bt'])
    if m:
        probs.append('C frames differ from bt -no-filters: ' + m)
    frames = doc['frames']
    for i, f in enumerate(frames):
        if f.get('synthetic') and (i == 0 or frames[i - 1]['kind'] !=
                                   'trace'):
            probs.append('synthetic VM frame #{} not after a trace frame'
                         .format(f['level']))
    if 'bt_raw' in rec:
        # Without the trace unwinder gdb stops at the mcode frame: what
        # it shows up to there must be the same.
        k = next(i for i, f in enumerate(frames) if f['kind'] == 'trace')
        mine = [norm_name(f) for f in frames[:k + 1]]
        raw = [b['name'] for b in rec['bt_raw'][:k + 1]]
        if mine != raw:
            probs.append('frames up to the trace differ from bt without '
                         'the unwinder: {} vs {}'.format(mine, raw))
    if not allow_warnings:
        w = warnings(doc)
        if w:
            probs.append('"!!" on a consistent stack: ' + '; '.join(w))
    links = [e['link'] for e in entries(doc) if e.get('link')]
    dups = set(x for x in links if links.count(x) > 1)
    if dups:
        probs.append('Lua frames shown twice: {}'.format(sorted(dups)))
    for s in doc['states']:
        if s['topnote'] is not None or s['broken'] or s['empty_top']:
            continue
        n = (rec.get('ljstack') or {}).get(s['L'])
        if n is not None and n != s['links'] + 1:
            probs.append('L={}: lj-stack has {} frames, lj-bt walked {} '
                         'links'.format(s['L'], n, s['links']))
        shown = len([e for e in entries(doc) if e.get('L') == s['L'] and
                     e['kind'] == 'lua'])
        if shown != s['links'] - s['hidden']:
            probs.append('L={}: {} Lua frames shown, {} links - {} hidden'
                         .format(s['L'], shown, s['links'], s['hidden']))
    return probs


def check_chain(ch, recs, oracle, tokens, oracle_tokens):
    '''Problems of one generated chain: invariants, the model, the
    oracle.'''
    if len(recs) != 1:
        return ['stopped {} times'.format(len(recs))]
    rec = recs[0]
    probs = check_doc(rec)
    if rec.get('error'):
        return probs
    doc = rec['ljbt']
    mine = tokens(entries(doc))
    in_trace = any(f['kind'] == 'trace' for f in doc['frames'])
    if not ch.jit:
        if mine != ch.model:
            probs.append('model mismatch:\n  lj-bt {}\n  model {}'.format(
                mine, ch.model))
    else:
        # Lines in and around traces come from snapshots: compare the
        # functions only.  Inside a trace the inlined frames are those of
        # the last exit before the pc: newer frames may be missing and a
        # frame may still be a function that tail-called since; so they
        # only have to be functions of the chain, and the real frames a
        # subsequence of the model.
        ents = entries(doc)

        def from_snapshot(e):
            # Inlined frames and the trace's base frame (its function and
            # line come from the snapshot, which may predate tail calls).
            return e.get('inlined') or 'base frame, snapshot' in (
                e.get('note') or '')
        inl = [e for e in ents if from_snapshot(e)]
        short = [t[:2] for t in tokens([e for e in ents
                                        if not from_snapshot(e)])]
        model = [t[:2] for t in ch.model]
        ok = is_subsequence(short, model) if in_trace else short == model
        if not ok:
            probs.append('model mismatch (jit{}):\n  lj-bt {}\n  model {}'
                         .format(', in a trace' if in_trace else '',
                                 short, model))
        builtins = set(t[1] for t in ch.model if t[0] == 'B')
        strange = [e['text'] for e in inl
                   if (e.get('fkind') == 'lua' and
                       e.get('defline') not in ch.deflines) or
                   (e.get('fkind') in ('c', 'ff') and
                    e.get('func') not in builtins) or e.get('fkind') is None]
        if strange:
            probs.append('inlined frames not of the chain: {}'.format(
                strange))
    if ch.oracle:
        levels = oracle.get(ch.seq)
        if levels is None:
            probs.append('no oracle record')
        else:
            cur = [t if t[0] != 'B' else ('B', None) for t in tokens(
                [e for e in entries(doc) if e.get('L') == doc['L']])]
            exp = oracle_tokens(levels, ch.target)
            if cur != exp:
                probs.append('oracle mismatch:\n  lj-bt  {}\n  oracle {}'
                             .format(cur, exp))
    return probs


def norm_name(f):
    if f['kind'] == 'sig':
        return '<signal handler called>'
    return f['name'] or '??'


RX_BT = re.compile(r'^#(\d+)\s+(?:0x[0-9a-f]+ in )?(.*?)(?: \(.*)?$')


def parse_bt(text):
    '''Frames of `bt -no-filters` output: [{level, name}].'''
    res = []
    for line in text.splitlines():
        m = RX_BT.match(line)
        if not m:
            continue
        name = m.group(2)
        if name.startswith('<signal handler called>'):
            name = '<signal handler called>'
        res.append({'level': int(m.group(1)), 'name': name})
    return res
