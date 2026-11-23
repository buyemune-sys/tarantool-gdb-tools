# Sourced into gdb after luajit_dbg.py: `dbg-enum-check` prints, as
# JSON, the members of every enum of luajit_dbg.py's EnumBasedList as
# the debug info has them and as its built-in ENUM_FALLBACK has them.
import json

import __main__ as m
import gdb


class EnumCheck(gdb.Command):
    def __init__(self):
        super(EnumCheck, self).__init__('dbg-enum-check', gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        out = {}
        for v in vars(m).values():
            if not isinstance(v, m.EnumBasedList):
                continue
            name = v._EnumBasedList__max_enum_member
            maxv = m.dbg.create_enum_value(name, v._EnumBasedList__enum_name)
            out[name] = {
                'dwarf': [str(m.dbg.cast_typeof(maxv, i))
                          for i in range(int(maxv))],
                'fallback': m.ENUM_FALLBACK.get(name, '').split(),
            }
        gdb.write(json.dumps(out) + '\n')


EnumCheck()
