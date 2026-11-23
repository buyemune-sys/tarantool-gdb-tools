# Sourced into gdb before luajit_dbg.py: makes the members listed in
# $DBG_HIDE_ENUMS (comma-separated) unknown to gdb.parse_and_eval, as if
# the debug info had not their enums (an enum used only through its
# constants may be left out of it, tarantool/tarantool#13094).
import os

import gdb

_HIDDEN = set(os.environ.get('DBG_HIDE_ENUMS', '').split(','))
_parse_and_eval = gdb.parse_and_eval


def _hiding_parse_and_eval(expr, *args, **kwargs):
    if expr.strip() in _HIDDEN:
        raise gdb.error('No symbol "{}" in current context.'.format(expr))
    return _parse_and_eval(expr, *args, **kwargs)


gdb.parse_and_eval = _hiding_parse_and_eval
