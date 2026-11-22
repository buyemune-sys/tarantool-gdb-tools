# gdb side of the `rust` group (tests/test_rust.py): sourced after probe.py.
#
#   ljbt-rust-probe FILE --tag TAG [--fiber ID] [--gcore DIR] [OPTS...]
#
# runs `ljbt-probe` (after `tt-fiber ID`, if given) with OPTS and appends
# its record to FILE, with:
#   tag      what the stop is, as the test names it
#   fiber    the fiber selected with tt-fiber, or null
#   bt       the frames of `bt -no-filters`, parsed so that a Rust name
#            with " (" in it stays whole (generics: `wrapper<F, (i64), i64>`
#            would be cut there by probe.py); with --fiber, without the
#            frame #0 of the running fiber, which lj-bt and the frame
#            filter of tarantool-gdb.py leave out
#   bt_probe the frames as probe.py parsed them
#   solibs   {pc: shared library or null} of the lj-bt frames
#   core     with --gcore, the core file `gcore` wrote into DIR
#   language [the language setting, the current language] before
#            ljbt-probe; language_after, the same after it, and
#            language_error after an `lj-bt` that fails (bad flags)
#
#   ljbt-rust-fibers FILE RESULTS
#
# at the stop of the fibers scenario of tests/lua/rust.lua (seq 8):
# ljbt-rust-probe of each fiber whose id the scenario wrote to RESULTS
# (results.jsonl), tagged fiber-lua and fiber-rust.
#
#   $ljbt_err_is(L, "text")
#
# for a breakpoint condition in lj_err_throw: the error thrown is a string
# with text in it.  The JIT throws errors too: an aborted trace recording
# throws the number of the TraceError (lj_trace_err).

import json
import os
import re

import gdb

import __main__ as M

RUST_RX_BT = re.compile(r'^#(\d+)\s+(?:0x[0-9a-f]+ in )?(.*) \([^()]*\)'
                        r'(?: (?:at|from) .*)?$')
RUST_RX_SIG = re.compile(r'^#(\d+)\s+(?:0x[0-9a-f]+ in )?<signal handler')
RUST_FIBERS_SEQ = 8


def rust_parse_bt(text):
    '''Frames of `bt -no-filters`: [{level, name}].  The name is all before
    the last parenthesized argument list (`set print frame-arguments
    none` keeps it free of parentheses).'''
    res = []
    for line in text.splitlines():
        m = RUST_RX_SIG.match(line)
        if m:
            res.append({'level': int(m.group(1)),
                        'name': '<signal handler called>'})
            continue
        m = RUST_RX_BT.match(line)
        if m:
            res.append({'level': int(m.group(1)), 'name': m.group(2)})
    return res


def rust_language():
    '''[`show language` setting, the language gdb uses now].'''
    cur = getattr(gdb, 'current_language', None)
    return [gdb.parameter('language'), cur() if cur else None]


def rust_reset_fiber():
    FU = getattr(M, 'FiberUnwinder', None)
    if FU is not None:
        FU.instance().reset_fiber()
    gdb.invalidate_cached_frames()


class RustProbeCommand(gdb.Command):
    def __init__(self):
        super(RustProbeCommand, self).__init__('ljbt-rust-probe',
                                               gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        argv = gdb.string_to_argv(arg)
        path, rest = argv[0], []
        tag, fiber, gcore = None, None, None
        i = 1
        while i < len(argv):
            if argv[i] == '--tag':
                tag = argv[i + 1]
                i += 2
            elif argv[i] == '--fiber':
                fiber = int(argv[i + 1])
                i += 2
            elif argv[i] == '--gcore':
                gcore = argv[i + 1]
                i += 2
            else:
                rest.append(argv[i])
                i += 1
        one = path + '.one'
        if os.path.exists(one):
            os.unlink(one)
        try:
            if fiber is not None:
                gdb.execute('tt-fiber {}'.format(fiber), False, True)
            lang = rust_language()
            gdb.execute('ljbt-probe {} {}'.format(one, ' '.join(rest)),
                        False, True)
            with open(one) as fh:
                rec = json.loads(fh.read().splitlines()[-1])
            os.unlink(one)
            rec['language'], rec['language_after'] = lang, rust_language()
            try:
                gdb.execute('lj-bt -n none', False, True)
            except gdb.error:
                pass
            rec['language_error'] = rust_language()
            try:
                text = gdb.execute('bt -no-filters', False, True)
            except gdb.error:
                text = ''
            bt = rust_parse_bt(text)
            if fiber is not None:
                bt = [b for b in bt if b['level'] != 0]
            rec['bt_probe'], rec['bt'] = rec['bt'], bt
            rec['solibs'] = {}
            for f in (rec['ljbt'] or {}).get('frames', []):
                rec['solibs'][f['pc']] = gdb.solib_name(int(f['pc'], 16))
        finally:
            if fiber is not None:
                rust_reset_fiber()
        rec['tag'], rec['fiber'] = tag, fiber
        if gcore:
            core = os.path.join(gcore, 'core.{}-{}'.format(rec['seq'], tag))
            try:
                gdb.execute('gcore ' + core, False, True)
                rec['core'] = core
            except gdb.error as e:
                rec['core_error'] = str(e)
        with open(path, 'a') as fh:
            fh.write(json.dumps(rec) + '\n')


class RustFibersCommand(gdb.Command):
    def __init__(self):
        super(RustFibersCommand, self).__init__('ljbt-rust-fibers',
                                                gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        path, results = gdb.string_to_argv(arg)
        if M.probe_seq() != RUST_FIBERS_SEQ or not os.path.exists(results):
            return
        ids = None
        with open(results) as fh:
            for line in fh:
                r = json.loads(line)
                if r.get('scenario') == 'fibers' and 'lua' in r:
                    ids = r
        if ids is None:
            return
        for kind in ('lua', 'rust'):
            gdb.execute('ljbt-rust-probe {} --tag fiber-{} --fiber {}'
                        .format(path, kind, ids[kind]), False, True)


class RustErrorFunction(gdb.Function):
    def __init__(self):
        super(RustErrorFunction, self).__init__('ljbt_err_is')

    def invoke(self, L, text):
        try:
            tn, s = M.ljbt_tv(int(L['top']) - M.LJBT_TV)
            return tn == 'LJ_TSTR' and text.string() in M.ljbt_gcstr(s)
        except gdb.error:
            return False


RustProbeCommand()
RustFibersCommand()
RustErrorFunction()
