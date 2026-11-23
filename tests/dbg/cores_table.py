#!/usr/bin/env python3
# cores_table.py FILE...: the DBG_CORES_SUMMARY files of test_cores.py as
# one Markdown table, a row per file.  A file in a directory
# cores-<label> (an artifact of the cores workflow) is labeled with
# <label>, the runner.
import json
import os
import sys

COLS = [('runner', 'runner'), ('os', 'OS'), ('arch', 'arch'),
        ('kernel', 'kernel'), ('glibc', 'glibc'), ('gdb', 'gdb'),
        ('guard_pages', 'guard pages in thread stacks'),
        ('gcore', 'gcore keeps the stacks'),
        ('gcore_pages', 'gcore-pages keeps them'),
        ('kernel_core', 'kernel core keeps them')]


def cell(row, key):
    v = row.get(key) or ''
    if key == 'gcore' and row.get('known'):
        v += ', expected: gdb bug 33855'
    if key in ('gcore', 'gcore_pages', 'kernel_core'):
        v = 'yes' if v == 'kept' else 'no: ' + v if v.startswith('lost') \
            else v
    return v.replace('|', '\\|')


def main():
    rows = []
    for path in sys.argv[1:]:
        with open(path) as fh:
            row = json.load(fh)
        d = os.path.basename(os.path.dirname(os.path.abspath(path)))
        row['runner'] = d[len('cores-'):] if d.startswith('cores-') else ''
        rows.append(row)
    rows.sort(key=lambda r: (r['arch'], r['runner'], r['os']))
    print('| ' + ' | '.join(name for _, name in COLS) + ' |')
    print('|' + '---|' * len(COLS))
    for r in rows:
        print('| ' + ' | '.join(cell(r, k) for k, _ in COLS) + ' |')
    known = sorted(set(r['known'] for r in rows if r.get('known')))
    if known:
        print()
        for k in known:
            print('- {}'.format(k))


main()
