# Sourced into gdb before luajit_dbg.py or tarantool-gdb.py: makes the
# members listed in $DBG_HIDE_ENUMS (comma-separated) unknown to
# gdb.parse_and_eval, as if the debug info had not their enums (an enum
# used only through its constants may be left out of it,
# tarantool/tarantool#13094), and the types of $DBG_HIDE_TYPES unknown
# to gdb.lookup_type.  The tests of tarantool-gdb.py also hide
# thread-local variables this way (cord_ptr, &cord_ptr...).
import os

import gdb

_HIDDEN = set(os.environ.get('DBG_HIDE_ENUMS', '').split(',')) - {''}
_HIDDEN_TYPES = set(os.environ.get('DBG_HIDE_TYPES', '').split(',')) - {''}
_parse_and_eval = gdb.parse_and_eval
_lookup_type = gdb.lookup_type


def _hiding_parse_and_eval(expr, *args, **kwargs):
    if expr.strip() in _HIDDEN:
        raise gdb.error('No symbol "{}" in current context.'.format(expr))
    return _parse_and_eval(expr, *args, **kwargs)


def _hiding_lookup_type(name, *args, **kwargs):
    if name.strip() in _HIDDEN_TYPES:
        raise gdb.error('No type named {}.'.format(name))
    return _lookup_type(name, *args, **kwargs)


gdb.parse_and_eval = _hiding_parse_and_eval
gdb.lookup_type = _hiding_lookup_type
