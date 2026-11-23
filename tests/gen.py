# Generator of call chains for the lj-bt tests (layers 1 and 2).
#
# A chain is F<seq>_0 -> F<seq>_1 -> ... -> F<seq>_n: every F_k moves to
# F_{k+1} by one transition (a plain call, a tail call, pcall, a
# coroutine, a table.sort comparator, a metamethod, another fiber, a hot
# loop, a vararg function, the reader of load(), a chunk of loadstring, a
# function of 70000 lines, a deep recursion, ...), F_n calls the target C
# function, where gdb stops.  Along
# with the Lua source the generator builds the expected Lua side of the
# stopped stack, oldest frame first:
#   ('L', defline, line)   a Lua function suspended at line
#   ('B', name)            a builtin or a Lua C function (lj-bt "func")
#   ('H', defline)         the tail call heuristic line for the function
#                          defined at defline
# Tail calls remove frames; the heuristic line is expected exactly when
# the head of a tail-call run was called from Lua through what the call
# site names (a global, pcall/xpcall of one, an upvalue, a field, a
# local, the iterator of a for-in loop; a reassigned global names what
# it holds now), not through __call nor as a coroutine body.
#
# Each token has its attributes in the parallel list Chain.attrs, which
# lj-bt must show in the fields of the entry:
#   ftype, how   ('L' and 'B') how the frame was called: the type of its
#                frame link and lj-bt's words for it (BY_* below)
#   chunk        ('L' and 'H') the chunk of the function; None is the
#                generated file itself (Chain.path, set by run_chains)
#   state        the lua_State, numbered within the chain: tokens of one
#                state share the "L" of their entries, others differ
#   co           the frame runs in the VM entry of coroutine.resume (the
#                VM frame says "coroutine"), not in one of a call from C
#   jit          the token is the function with the hot loop, or newer:
#                under JIT its frames may come from a trace or be called
#                from one, only the older ones are certain

import random

# How a frame was called: (ftype, how) of lj-bt.
BY_LUA = ('L', [])
BY_PCALL = ('PP', ['protected by pcall/xpcall'])
BY_C = ('C', ['called from C: lua_call'])
BY_CPCALL = ('CP', ['called from C: lua_pcall/cpcall'])
BY_RESUME = ('CP', ['coroutine body'])
BY_META = ('M', ['metamethod, continuation lj_cont_ra'])
BY_FFI_CALLBACK = ('M', ['FFI callback'])

TRANSITIONS = ['call', 'tail', 'pcall', 'xpcall', 'resume', 'wrap', 'sort',
               'gsub', 'index', 'callmt', 'add', 'fiber', 'jit', 'xerror',
               'qsort', 'vararg', 'load', 'chunk', 'tailup', 'tailfield',
               'taillocal', 'tailiter', 'tailmt', 'tailre', 'long', 'deep']

# At most one of these per random chain.
ONCE = ('jit', 'xerror', 'long', 'deep')

# 'deep': the depth of the recursion; 'long': the lines between the start
# of the function and its call, by the width of its line info (lj_parse.c
# fs_fixup_line: 8, 16 or 32 bits).
DEPTH = 5000
LONG = (300, 70000)


def shortname(src):
    '''The chunk name lj-bt and LuaJIT's short_src give a chunk loaded
    from a string (lj_debug_shortname, LUA_IDSIZE 60); a file ("@") is
    its whole path in lj-bt.'''
    if src.startswith('@'):
        return src[1:]
    if src.startswith('='):
        return src[1:60]
    n = 0
    while n < 48 and n < len(src) and src[n] >= ' ':
        n += 1
    if n < len(src):
        return '[string "{}..."]'.format(src[:min(n, 45)])
    return '[string "{}"]'.format(src)


def lua_quote(s):
    '''s as a one-line Lua string literal.'''
    return "'{}'".format(s.replace('\\', '\\\\').replace("'", "\\'")
                         .replace('\n', '\\n'))


class Chain(object):
    def __init__(self, seq, transitions, target, oracle, hot=False,
                 depth=DEPTH):
        self.seq = seq
        self.depth = depth          # Of the recursion of 'deep'.
        self.transitions = transitions
        self.target = target        # 'c' (lbox_session_exists) or 'ffi'
        self.oracle = oracle and 'jit' not in transitions
        # The hot loop of 'jit' makes its last call (the one that goes on
        # down the chain) only once the loop runs in a trace (H.hot): for
        # chains that must stop in one.  Else after 100 iterations, which
        # is usually, not surely, past the start of the trace.
        self.hot = hot
        self.model = []             # Expected tokens, oldest first.
        self.attrs = []             # Their attributes (the header).
        self.path = None            # The generated file (run_chains).
        self.loaded = set()         # (chunk, defline) of loaded functions.
        self.jit = 'jit' in transitions
        self.has_fiber = 'fiber' in transitions

    def name(self, k):
        return 'F{}_{}'.format(self.seq, k)


def random_chain(seq, rng, oracle=True):
    n = rng.randint(1, 6)
    tr = []
    for _ in range(n):
        t = rng.choice(TRANSITIONS)
        # One hot loop is enough (100^2 runs otherwise); an error raised
        # inside an xpcall handler is "error in error handling", the
        # inner handler never runs.
        # An FFI callback cannot run under a JIT-compiled FFI call ("bad
        # callback"), so no qsort after a hot loop.  One long function
        # (70000 lines) and one deep recursion keep a batch small.
        while (t in ONCE and t in tr) or (t == 'qsort' and 'jit' in tr):
            t = rng.choice(TRANSITIONS)
        tr.append(t)
    # lj-bt takes about 3 s for the 5000 frames (x86_64): one deep chain
    # in four is that deep, the others up to 300.
    depth = DEPTH if rng.random() < 0.25 else rng.randint(2, 300)
    return Chain(seq, tr, rng.choice(['c', 'ffi']), oracle, depth=depth)


class Program(object):
    '''One Lua file with many chains and a driver calling them in turn.'''

    def __init__(self, jit_on, driver='main'):
        self.jit_on = jit_on
        self.driver = driver
        self.lines = []
        self.chains = []

    def emit(self, s):
        self.lines.append(s)
        return len(self.lines)

    def add(self, ch):
        self.chains.append(ch)

    def render(self):
        e = self.emit
        e('-- Generated by tests/gen.py: {} chains, jit {}, driver {}.'.format(
            len(self.chains), 'on' if self.jit_on else 'off', self.driver))
        e("local H = dofile(os.getenv('LJBT_LUA_DIR') .. '/chain.lua')")
        e("local ffi = require('ffi')")
        e("local C = ffi.C")
        e("local fiber = require('fiber')")
        e('H.setup({})'.format('true' if self.jit_on else 'false'))
        for ch in self.chains:
            self.render_chain(ch)
        dline = e('local function D()')
        calls = {}
        for ch in self.chains:
            calls[ch.seq] = e('  local ok, err = pcall({}, {}) '
                              'H.check(ok, err, {})'.format(
                                  ch.name(0), 4000 + ch.seq, ch.seq))
        e('end')
        if self.driver == 'fiber':
            mline = e("local f = fiber.new(D) f:name('driver') "
                      "f:set_joinable(true) f:join()")
        else:
            mline = e('D()')
        e('os.exit(0)')
        for ch in self.chains:
            # The main chunk is run by lua_pcall (luaT_call), D by the
            # main chunk or as the body of the driver fiber.
            bottom = []
            if self.driver == 'main':
                bottom.append((('L', 0, mline), BY_CPCALL))
                bottom.append((('L', dline, calls[ch.seq]), BY_LUA))
            else:
                bottom.append((('L', dline, calls[ch.seq]), BY_CPCALL))
            bottom.append((('B', 'pcall'), BY_LUA))
            # Fiber transitions reset the stack: keep the last part.  The
            # part before is the stack of the creator: fiber.create runs
            # the new fiber at once, the creator waits in it, at the line
            # of its call.
            full = [t for t, _ in bottom] + ch.model
            fulla = [dict(ftype=by[0], how=by[1], chunk=None, state=0,
                          co=False, jit=False) for _, by in bottom] + \
                ch.attrs
            ch.parents = []
            start = 0
            for i, t in enumerate(full):
                if t == ('RESET',):
                    ch.parents.append(full[start:i] +
                                      [('B', 'lbox_fiber_create')])
                    start = i + 1
            ch.model = full[start:]
            ch.attrs = fulla[start:]
        return '\n'.join(self.lines) + '\n'

    def render_chain(self, ch):
        start = len(self.lines)
        self.render_chain_body(ch)
        # Every function of the chain, tail-called ones too: what an
        # inlined frame from a (lagging) snapshot may legitimately show.
        ch.deflines = set(i + 1 for i in range(start, len(self.lines))
                          if 'function' in self.lines[i])

    def render_chain_body(self, ch):
        e = self.emit
        if ch.target == 'ffi':
            e('H.name({})'.format(ch.seq))
        model, attrs = [], []
        st = dict(state=0, co=False, jit=False)

        def put(tok, by=None, chunk=None):
            if by in (BY_C, BY_FFI_CALLBACK):
                st['co'] = False        # Called from C: a new VM entry.
            model.append(tok)
            attrs.append(dict(st, ftype=by[0] if by else None,
                              how=by[1] if by else None, chunk=chunk))

        def new_state(co):
            st.update(state=st['state'] + 1, co=co)

        # How F_k is called and whether its call site names it.
        incoming = (BY_PCALL, True)     # F_0 is called by pcall in D().
        pending = None              # (defline, resolvable) of a tail run.
        n = len(ch.transitions)
        for k in range(n + 1):
            F = ch.name(k)
            N = ch.name(k + 1)
            if k == n:
                d = e('function _G.{}(x)'.format(F))
                pre = 'H.oracle({}) '.format(ch.seq) if ch.oracle else ''
                if ch.target == 'c':
                    line = e('  {}local r = box.session.exists(x) return r'
                             .format(pre))
                else:
                    line = e('  {}local nm = NAMES[x] local r = '
                             'C.box_space_id_by_name(nm, #nm) return r'
                             .format(pre))
                e('end')
                if pending and pending[1]:
                    put(('H', pending[0]))
                put(('L', d, line), incoming[0])
                put(('B', 'lbox_session_exists' if ch.target == 'c'
                     else 'ffi.meta.__call'), BY_LUA)
                break
            t = ch.transitions[k]
            # The tail hops with an upvalue: a block keeps the main chunk
            # under its 200 locals.
            after = []
            if t == 'tailup':
                e('do local w')
                after = ['end']
            d = e('function _G.{}(x{})'.format(F, ', ...' if t == 'vararg'
                                                 else ''))
            if t == 'tail':
                e('  return {}(x)'.format(N))
                e('end')
                if pending is None:
                    pending = (d, incoming[1])
                continue
            # Tokens after F_k's own, with how each was called; ('NEW',
            # co) starts another lua_State.  next_pending: the tail run a
            # hop starts itself, the head W tail-calling F_{k+1}:
            # (defline the heuristic line names, resolvable).
            extra = []
            nxt = (BY_LUA, True)
            next_pending = None
            if t == 'jit':
                st['jit'] = True
            if t == 'call':
                s = e('  local r = {}(x)'.format(N))
                e('  return r')
            elif t == 'vararg':
                # Its frame has a FRAME_VARG link over the one of the call.
                s = e('  local r = {}(x, ...)'.format(N))
                e('  return r')
            elif t == 'load':
                # load() with a reader: lua_load runs the parser under
                # lj_vm_cpcall, a cframe with nres < 0 that owns no frame;
                # the parser calls the reader by lua_call (reader_func).
                e('  local r')
                e('  local done = false')
                dr = e('  local function reader()')
                e('    if done then return nil end')
                e('    done = true')
                sr = e('    r = {}(x)'.format(N))
                e("    return 'return 1'")
                e('  end')
                s = e("  local f = load(reader, '=ljbt')")
                e('  return r')
                extra = [(('B', 'load'), BY_LUA), (('L', dr, sr), BY_C)]
            elif t == 'chunk':
                # A function of a chunk of its own: loadstring with a name
                # ("=name") or with the source as the name ([string ...],
                # cut after 45 characters of the first line).
                src = ['-- ljbt chunk {}: a chunk loaded from a string, the '
                       'first line of which is long'.format(F),
                       'return function(x)',
                       '  local r = {}(x)'.format(N),
                       '  return r',
                       'end']
                if (ch.seq + k) % 2 == 0:
                    name = '=ljbt.{}'.format(F)
                    e('  local g = loadstring({}, {})()'.format(
                        lua_quote('\n'.join(src)), lua_quote(name)))
                else:
                    name = '\n'.join(src)
                    e('  local g = loadstring({})()'.format(lua_quote(name)))
                s = e('  local r = g(x)')
                e('  return r')
                chunk = shortname(name)
                ch.loaded.add((chunk, 2))
                extra = [(('L', 2, 3), BY_LUA, chunk)]
            elif t in ('tailup', 'tailfield', 'taillocal', 'tailiter'):
                # W, the head of a tail run, called through an upvalue, a
                # field, a local or as the iterator of ITERC: the heuristic
                # resolves each to W.
                dw = e('  {} function(y) return {}(y) end'.format({
                    'tailup': 'w = w or',
                    'tailfield': 'local t = {w =',
                    'taillocal': 'local w =',
                    'tailiter': 'local w =',
                }[t], N) + ('}' if t == 'tailfield' else ''))
                if t == 'tailiter':
                    e('  local r')
                    s = e('  for v in w, x do r = v break end')
                else:
                    s = e('  local r = {}(x)'.format(
                        't.w' if t == 'tailfield' else 'w'))
                e('  return r')
                next_pending = (dw, True)
            elif t == 'tailmt':
                # The head W is __call of a table: the call site has the
                # table, nothing to verify, no heuristic line.
                dw = e('  local t = setmetatable({{}}, {{__call = '
                       'function(_, y) return {}(y) end}})'.format(N))
                s = e('  local r = t(x)')
                e('  return r')
                next_pending = (dw, False)
            elif t == 'tailre':
                # W is called through the global G, reassigns it and
                # tail-calls F_{k+1}: the heuristic names what G holds now,
                # X (README: wrong when the global was reassigned).
                dx = e('  local function X(y) return y end')
                e('  local function W(y) G{} = X return {}(y) end'.format(
                    F, N))
                e('  G{} = W'.format(F))
                s = e('  local r = G{}(x)'.format(F))
                e('  return r')
                next_pending = (dx, True)
            elif t == 'long':
                # The call far from the start of the function: line info
                # of 16 or 32 bits (the next functions start past 65536).
                e('  local r')
                self.lines.extend([''] * LONG[(ch.seq + k) % 2])
                s = e('  r = {}(x)'.format(N))
                e('  return r')
            elif t == 'deep':
                # depth + 1 frames of rec between F_k and F_{k+1}.
                dr = e('  local function rec(n)')
                s0 = e('    if n == 0 then local r = {}(x) return r end'
                       .format(N))
                sr = e('    local r = rec(n - 1)')
                e('    return r')
                e('  end')
                s = e('  local r = rec({})'.format(ch.depth))
                e('  return r')
                extra = [(('L', dr, sr), BY_LUA)] * ch.depth + \
                    [(('L', dr, s0), BY_LUA)]
            elif t in ('pcall', 'xpcall'):
                if t == 'pcall':
                    s = e('  local ok, r = pcall({}, x)'.format(N))
                else:
                    s = e('  local ok, r = xpcall({}, debug.traceback, x)'
                          .format(N))
                e('  return r')
                extra = [(('B', t), BY_LUA)]
                nxt = (BY_PCALL, True)
            elif t in ('resume', 'wrap'):
                if t == 'resume':
                    e('  local co = coroutine.create({})'.format(N))
                    s = e('  local ok, r = coroutine.resume(co, x)')
                    extra = [(('B', 'coroutine.resume'), BY_LUA)]
                else:
                    e('  local w = coroutine.wrap({})'.format(N))
                    s = e('  local r = w(x)')
                    extra = [(('B', 'coroutine.wrap_aux'), BY_LUA)]
                e('  return r')
                extra.append((('NEW', True), None))
                nxt = (BY_RESUME, False)
            elif t in ('sort', 'gsub'):
                e('  local r')
                if t == 'sort':
                    dc = e('  local function cmp(a, b)')
                    sc = e('    r = {}(x)'.format(N))
                    e('    return a < b')
                    e('  end')
                    s = e('  table.sort({2, 1}, cmp)')
                    extra = [(('B', 'table.sort'), BY_LUA),
                             (('L', dc, sc), BY_C)]
                else:
                    dc = e('  local function cb(m)')
                    sc = e('    r = {}(x)'.format(N))
                    e('    return m')
                    e('  end')
                    s = e("  string.gsub('a', 'a', cb)")
                    extra = [(('B', 'string.gsub'), BY_LUA),
                             (('L', dc, sc), BY_C)]
                e('  return r')
            elif t in ('index', 'callmt', 'add'):
                mm, args, use = {
                    'index': ('__index', '_, key', 't.key'),
                    'callmt': ('__call', '_, y', 't(x)'),
                    'add': ('__add', 'a, b', 't + 1'),
                }[t]
                dm = e('  local function mm({})'.format(args))
                sm = e('    local v = {}({})'.format(
                    N, 'y' if t == 'callmt' else 'x'))
                e('    return v')
                e('  end')
                e('  local t = setmetatable({{}}, {{{} = mm}})'.format(mm))
                s = e('  local r = {}'.format(use))
                e('  return r')
                # __call is a plain call of mm (lj_meta_call), the others
                # a continuation frame.
                extra = [(('L', dm, sm),
                          BY_LUA if t == 'callmt' else BY_META)]
            elif t == 'fiber':
                e('  local ch = fiber.channel(1)')
                db = e('  local function body()')
                sb = e('    ch:put({}(x))'.format(N))
                e('  end')
                s = e('  fiber.create(body)')
                e('  local r = ch:get()')
                e('  return r')
                extra = [(('RESET',), None), (('NEW', False), None),
                         (('L', db, sb), BY_CPCALL)]
            elif t == 'jit' and ch.hot:
                # Rounds of the hot loop until it runs in a trace (at most
                # 30, 3000 iterations: a hot counter up to 6000); then its
                # last iteration goes on down the chain, in the trace.  The
                # rounds are a repeat loop: H.hot looks at the last FORL.
                e('  local args = {}')
                e('  for i = 1, 100 do args[i] = 2000 end')
                e('  local r')
                e('  local round = 0')
                e('  repeat')
                e('    round = round + 1')
                e('    if round == 30 or H.hot({}) then args[100] = x end'
                  .format(F))
                e('    for i = 1, 100 do')
                s = e('      r = {}(args[i])'.format(N))
                e('    end')
                e('  until args[100] == x')
                e('  H.snaps({}, {})'.format(ch.seq, F))
                e('  return r')
            elif t == 'jit':
                # NAMES[2000] and NAMES[4000 + seq] are both in the hash
                # part: no guard on the key leaves the trace at i == 100.
                e('  local args = {}')
                e('  for i = 1, 99 do args[i] = 2000 end')
                e('  args[100] = x')
                e('  local r')
                e('  for i = 1, 100 do')
                s = e('    r = {}(args[i])'.format(N))
                e('  end')
                e('  return r')
            elif t == 'xerror':
                dt = e("  local function thrower() error('boom') end")
                dh = e('  local function handler(err)')
                sh = e('    local v = {}(x)'.format(N))
                e('    return v')
                e('  end')
                s = e('  local ok, r = xpcall(thrower, handler)')
                e('  return r')
                # lj_err_run calls the handler with lj_vm_call.
                extra = [(('B', 'xpcall'), BY_LUA),
                         (('L', dt, dt), BY_PCALL),
                         (('B', 'error'), BY_LUA),
                         (('L', dh, sh), BY_C)]
            elif t == 'qsort':
                # A Lua function as an FFI callback (lj_ccallback.c): libc
                # qsort calls it back, its frame is a CONT one with
                # LJ_CONT_FFI_CALLBACK, a C frame for err_unwind.
                e('  local r')
                dc = e('  local function cmp(a, b)')
                sc = e('    r = {}(x)'.format(N))
                e('    return 0')
                e('  end')
                e("  local cb = ffi.cast('int (*)(const void *, "
                  "const void *)', cmp)")
                e("  local arr = ffi.new('int[2]', 2, 1)")
                s = e('  C.qsort(arr, 2, 4, cb)')
                e('  cb:free()')
                e('  return r')
                extra = [(('B', 'ffi.meta.__call'), BY_LUA),
                         (('L', dc, sc), BY_FFI_CALLBACK)]
            else:
                raise ValueError(t)
            e('end')
            for line in after:
                e(line)
            if pending and pending[1]:
                put(('H', pending[0]))
            pending = next_pending
            put(('L', d, s), incoming[0])
            for x in extra:
                if x[0][0] == 'NEW':
                    new_state(x[0][1])
                else:
                    put(*x)
            incoming = nxt
        ch.model = model
        ch.attrs = attrs


def tokens(entries):
    '''lj-bt entries (newest first) as model tokens, oldest first.'''
    res = []
    for e in reversed(entries):
        if e['kind'] == 'heuristic':
            if not e.get('probably'):
                res.append(('H', e['defline']))
        elif e.get('fkind') == 'lua':
            res.append(('L', e['defline'], e['currentline']))
        elif e.get('fkind') in ('c', 'ff'):
            res.append(('B', e['func']))
        else:
            res.append(('?', e.get('text')))
    return res


def oracle_tokens(levels, target):
    '''debug.getinfo levels (newest first) as tokens, oldest first, plus
    the target the oracle's caller is about to call.'''
    res = []
    for lv in reversed(levels):
        if lv['what'] in ('Lua', 'main'):
            res.append(('L', lv['linedefined'], lv['currentline']))
        else:
            res.append(('B', None))
    res.append(('B', None))
    return res
