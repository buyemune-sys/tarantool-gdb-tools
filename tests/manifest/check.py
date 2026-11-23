#!/usr/bin/env python3
# Checks of extensions.json, the manifest of the debugger extensions of
# this repo (README, "The manifest"):
#
#   tests/manifest/check.py [--manifest FILE] static
#   tests/manifest/check.py [--manifest FILE] gdb|lldb
#
# static: the schema; every file exists; the `after` order can be
# satisfied (known files, no cycle, each one loadable in the debuggers,
# versions and architectures of the file that comes after it); the CI
# matrix of the workflow that checks a file in a debugger (COVERAGE)
# has every minor from tarantool.min to tarantool.max on each arch, and
# that workflow runs this check in that debugger.
#
# gdb, lldb: loads each file of the manifest that lists the debugger
# into it, after the files of its `after` (and theirs), with the binary
# of $LJBT_TARANTOOL as the target, and compares the commands and
# parameters it registered, read back from the debugger
# (tests/manifest/probe.py), with the manifest: exactly.  A file whose
# range or arch does not have the binary is skipped.  DBG_GDB and
# DBG_LLDB are the debugger binaries (default gdb, lldb); TMPDIR.
#
# Exit status 1 on any error, each error on a line.
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MANIFEST = os.path.join(ROOT, 'extensions.json')
PROBE = os.path.join(HERE, 'probe.py')
WORKFLOWS = os.path.join(ROOT, '.github', 'workflows')

DEBUGGERS = ('gdb', 'lldb')
ARCHES = {'amd64': 'x86_64', 'arm64': 'aarch64'}
REQUIRED = {'file': str, 'debuggers': list, 'after': list,
            'tarantool': dict, 'arch': list, 'commands': list}
OPTIONAL = {'parameters': list}

# The workflow and job that run a file in a debugger, all the matrix of
# that job: the range and the architectures it shows.
COVERAGE = {
    ('luajit_gdb.py', 'gdb'): ('dbg.yml', 'dbg'),
    ('luajit_gdb.py', 'lldb'): ('lldb.yml', 'lldb'),
    ('tarantool_gdb.py', 'gdb'): ('dbg.yml', 'dbg'),
    ('luajit_backtrace_gdb.py', 'gdb'): ('test.yml', 'test'),
    # The cores of tests/dbg are written with gcore-pages.
    ('gcore_pages_gdb.py', 'gdb'): ('dbg.yml', 'dbg'),
}


def version(s):
    '''(major, minor, patch) of "X.Y.Z", or None.'''
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)$', s) if isinstance(s, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def load(path):
    with open(path) as fh:
        return json.load(fh)


def schema_errors(man):
    errs = []
    if not isinstance(man, dict) or set(man) != {'schema', 'extensions'}:
        return ['the manifest is {"schema": 1, "extensions": [...]}']
    if man['schema'] != 1:
        errs.append('schema {!r}: only 1 is known'.format(man['schema']))
    if not isinstance(man['extensions'], list) or not man['extensions']:
        return errs + ['extensions: a list of files']
    files = []
    for i, ext in enumerate(man['extensions']):
        where = 'extensions[{}]'.format(i)
        if not isinstance(ext, dict):
            errs.append(where + ': an object')
            continue
        where = ext.get('file') if isinstance(ext.get('file'), str) else where
        for key in sorted(set(ext) - set(REQUIRED) - set(OPTIONAL)):
            errs.append('{}: unknown key {}'.format(where, key))
        for key, t in sorted(REQUIRED.items()) + sorted(OPTIONAL.items()):
            if key not in ext:
                if key in REQUIRED:
                    errs.append('{}: no {}'.format(where, key))
                continue
            if not isinstance(ext[key], t):
                errs.append('{}: {} is a {}'.format(where, key, t.__name__))
            elif t is list:
                if not all(isinstance(x, str) and x for x in ext[key]):
                    errs.append('{}: {} has strings only'.format(where, key))
                elif len(set(ext[key])) != len(ext[key]):
                    errs.append('{}: {} has duplicates'.format(where, key))
        if isinstance(ext.get('file'), str):
            files.append(ext['file'])
        for d in ext.get('debuggers') or []:
            if d not in DEBUGGERS:
                errs.append('{}: unknown debugger {}'.format(where, d))
        if isinstance(ext.get('debuggers'), list) and not ext['debuggers']:
            errs.append('{}: no debugger'.format(where))
        for a in ext.get('arch') or []:
            if a not in ARCHES.values():
                errs.append('{}: unknown arch {}'.format(where, a))
        if isinstance(ext.get('arch'), list) and not ext['arch']:
            errs.append('{}: no arch'.format(where))
        tt = ext.get('tarantool')
        if isinstance(tt, dict):
            if set(tt) != {'min', 'max'}:
                errs.append(where + ': tarantool is {"min": V, "max": V}')
            elif not version(tt['min']) or not version(tt['max']):
                errs.append(where + ': tarantool versions are X.Y.Z')
            elif version(tt['min']) > version(tt['max']):
                errs.append(where + ': tarantool.min is above max')
            elif version(tt['min'])[0] != version(tt['max'])[0]:
                errs.append(where + ': tarantool range spans majors')
    if len(set(files)) != len(files):
        errs.append('a file is listed twice')
    return errs


def by_file(man):
    return {ext['file']: ext for ext in man['extensions']}


def closure(man, name, seen=()):
    '''The files to load before name, in order: its `after`, each after
    its own `after`.  Raises ValueError on an unknown file or a cycle.'''
    exts = by_file(man)
    order = []
    for dep in exts[name]['after']:
        if dep in seen or dep == name:
            raise ValueError('a cycle through {}'.format(dep))
        if dep not in exts:
            raise ValueError('{} is not in the manifest'.format(dep))
        for f in closure(man, dep, seen + (name,)) + [dep]:
            if f not in order:
                order.append(f)
    return order


def after_errors(man):
    errs = []
    exts = by_file(man)
    for name, ext in exts.items():
        try:
            deps = closure(man, name)
        except ValueError as e:
            errs.append('{}: after: {}'.format(name, e))
            continue
        for dep in deps:
            d = exts[dep]
            for dbg in ext['debuggers']:
                if dbg not in d['debuggers']:
                    errs.append('{}: after {}, which has no {}'.format(
                        name, dep, dbg))
            for a in ext['arch']:
                if a not in d['arch']:
                    errs.append('{}: after {}, which has no {}'.format(
                        name, dep, a))
            if version(d['tarantool']['min']) > \
                    version(ext['tarantool']['min']) or \
                    version(d['tarantool']['max']) < \
                    version(ext['tarantool']['max']):
                errs.append('{}: after {}, whose tarantool range is '
                            'narrower'.format(name, dep))
    return errs


def yaml_list(text):
    '''The list of a matrix line: a fromJSON('[...]') of the "all" case
    or a flow list [a, b].'''
    m = re.search(r"'(\[[^']*\])'", text)
    if m:
        return [str(x) for x in json.loads(m.group(1))]
    m = re.match(r'^\s*\[(.*)\]\s*$', text)
    if m:
        return [x.strip().strip('\'"') for x in m.group(1).split(',')
                if x.strip()]
    return None


def job_text(wf_text, job):
    '''The lines of a job of a workflow (2-space indented under jobs:).'''
    m = re.search(r'(?ms)^jobs:\n(.*)', wf_text)
    if not m:
        return None
    lines, cur = [], None
    for line in m.group(1).split('\n'):
        j = re.match(r'^  ([\w-]+):\s*$', line)
        if j:
            cur = j.group(1)
        elif re.match(r'^\S', line):
            break
        if cur == job:
            lines.append(line)
    return '\n'.join(lines) if lines else None


def matrix(wf_path, job):
    '''(versions, arches) of the matrix of a job, the arches as uname -m
    says them; raises ValueError.'''
    with open(wf_path) as fh:
        text = job_text(fh.read(), job)
    if text is None:
        raise ValueError('no job {}'.format(job))
    m = re.search(r'(?m)^ +matrix:\n((?: {8,}.*\n)+)', text + '\n')
    if not m:
        raise ValueError('job {} has no matrix'.format(job))
    keys = {}
    for line in m.group(1).split('\n'):
        k = re.match(r'^ {8}(\w+): (.*)$', line)
        if k:
            keys[k.group(1)] = yaml_list(k.group(2))
    if not keys.get('version'):
        raise ValueError('job {} has no matrix version list'.format(job))
    if not keys.get('arch'):
        raise ValueError('job {} has no matrix arch list'.format(job))
    vers = [version(v) for v in keys['version']]
    if None in vers:
        raise ValueError('job {}: a version is not X.Y.Z'.format(job))
    arches = [ARCHES.get(a, a) for a in keys['arch']]
    return vers, arches, text


def coverage_errors(man):
    errs = []
    for name, ext in by_file(man).items():
        lo = version(ext['tarantool']['min'])
        hi = version(ext['tarantool']['max'])
        for dbg in ext['debuggers']:
            where = '{} in {}'.format(name, dbg)
            if (name, dbg) not in COVERAGE:
                errs.append(where + ': no workflow checks it (COVERAGE '
                            'of tests/manifest/check.py)')
                continue
            wf, job = COVERAGE[(name, dbg)]
            try:
                vers, arches, text = matrix(os.path.join(WORKFLOWS, wf), job)
            except (OSError, ValueError) as e:
                errs.append('{}: {}: {}'.format(where, wf, e))
                continue
            for v, which in ((lo, 'min'), (hi, 'max')):
                if v not in vers:
                    errs.append('{}: tarantool.{} {} is not in the matrix '
                                'of {}'.format(where, which,
                                               '.'.join(map(str, v)), wf))
            for minor in range(lo[1], hi[1] + 1):
                if not any(v[:2] == (lo[0], minor) for v in vers):
                    errs.append('{}: no {}.{} in the matrix of {}'.format(
                        where, lo[0], minor, wf))
            for a in ext['arch']:
                if a not in arches:
                    errs.append('{}: no {} in the matrix of {}'.format(
                        where, a, wf))
            if not re.search(r'tests/manifest/check\.py\b[^\n]* {}\b'.format(
                    dbg), text):
                errs.append('{}: {} does not run tests/manifest/check.py '
                            '{}'.format(where, wf, dbg))
    return errs


def load_errors(man, root=ROOT):
    '''What makes the files of the manifest unloadable in its order: the
    schema, the files, the `after` order.'''
    errs = schema_errors(man)
    if errs:
        return errs
    for name in by_file(man):
        if os.path.basename(name) != name or \
                not os.path.isfile(os.path.join(root, name)):
            errs.append('{}: no such file in the repo root'.format(name))
    return errs + after_errors(man)


def static_errors(man, root=ROOT):
    errs = load_errors(man, root)
    if errs and schema_errors(man):
        return errs
    return errs + coverage_errors(man)


def tarantool_info(binary):
    '''((major, minor, patch), arch as uname -m says it) of the binary.'''
    out = subprocess.run([binary, '-e', 'print(_TARANTOOL, jit.arch) '
                          'os.exit(0)'], stdout=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, universal_newlines=True,
                         timeout=60).stdout.split()
    ver = version(out[0].split('-')[0])
    return ver, {'x64': 'x86_64', 'arm64': 'aarch64'}.get(out[1], out[1])


def probe(debugger, binary, sources, tmp):
    '''What each of sources registered, loaded in order into debugger:
    the result of probe.py.'''
    plan = {'binary': binary, 'sources': sources,
            'out': os.path.join(tmp, 'result.json')}
    with open(os.path.join(tmp, 'plan.json'), 'w') as fh:
        json.dump(plan, fh)
    env = dict(os.environ, MANIFEST_PLAN=os.path.join(tmp, 'plan.json'),
               PYTHONDONTWRITEBYTECODE='1')
    if debugger == 'gdb':
        argv = [os.environ.get('DBG_GDB', 'gdb'), '-batch', '-nx', '-q',
                '-iex', 'set auto-load python-scripts off', '-x', PROBE]
    else:
        argv = [os.environ.get('DBG_LLDB', 'lldb'), '--batch',
                '--no-lldbinit', '-o', 'command script import ' + PROBE]
    p = subprocess.run(argv, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                       universal_newlines=True, timeout=180, env=env,
                       cwd=tmp)
    try:
        res = load(plan['out'])
    except (OSError, ValueError):
        raise RuntimeError('{} wrote no result:\n{}'.format(debugger,
                                                            p.stdout))
    if res['fatal']:
        raise RuntimeError(res['fatal'])
    return res['loads']


def registered(man, debugger, binary=None):
    '''{file: load of probe.py} for every file of the manifest that lists
    the debugger and whose range and arch have the binary; None for the
    skipped ones.'''
    binary = binary or os.environ.get('LJBT_TARANTOOL')
    if not binary:
        raise RuntimeError('set LJBT_TARANTOOL to a tarantool binary')
    ver, arch = tarantool_info(binary)
    res = {}
    for name, ext in by_file(man).items():
        if debugger not in ext['debuggers']:
            continue
        if arch not in ext['arch'] or \
                not version(ext['tarantool']['min']) <= ver <= \
                version(ext['tarantool']['max']):
            res[name] = None
            continue
        tmp = tempfile.mkdtemp(prefix='manifest-')
        try:
            files = closure(man, name) + [name]
            loads = probe(debugger, binary,
                          [os.path.join(ROOT, f) for f in files], tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        res[name] = {'deps': loads[:-1], 'self': loads[-1]}
    return res


def compare(man, debugger, real):
    '''Errors of the manifest against registered().'''
    errs = []
    for name, ext in by_file(man).items():
        if debugger not in ext['debuggers'] or real.get(name) is None:
            continue
        where = '{} in {}'.format(name, debugger)
        for dep in real[name]['deps']:
            if dep['error']:
                errs.append('{}: loading {} (after) failed: {}'.format(
                    where, dep['file'], dep['error']))
        got = real[name]['self']
        if got['error']:
            errs.append('{}: loading failed: {}'.format(where, got['error']))
        for key in ('commands', 'parameters'):
            want = set(ext.get(key, []))
            have = set(got[key])
            if have - want:
                errs.append('{}: registers {} the manifest has not: {}'.format(
                    where, key, ', '.join(sorted(have - want))))
            if want - have:
                errs.append('{}: does not register {} of the manifest: '
                            '{}'.format(where, key,
                                        ', '.join(sorted(want - have))))
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', default=MANIFEST)
    ap.add_argument('what', choices=('static',) + DEBUGGERS)
    args = ap.parse_args()
    man = load(args.manifest)
    if args.what == 'static':
        errs = static_errors(man)
        n = 'the manifest'
    else:
        errs = load_errors(man)
        if not errs:
            real = registered(man, args.what)
            for name, r in sorted(real.items()):
                if r is None:
                    print('skip {}: not for this binary'.format(name))
                else:
                    print('{} in {}: commands {}; parameters {}'.format(
                        name, args.what, ', '.join(r['self']['commands']),
                        ', '.join(r['self']['parameters']) or '-'))
            errs = compare(man, args.what, real)
        n = 'the manifest in ' + args.what
    for e in errs:
        print('error: ' + e)
    print('{}: {}'.format(n, 'FAILED' if errs else 'OK'))
    sys.exit(1 if errs else 0)


if __name__ == '__main__':
    main()
