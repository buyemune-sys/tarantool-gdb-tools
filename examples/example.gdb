# Example: stop in a C function deep in a mixed C/Lua stack and print it.
# Run from the root of the repository:
#   gdb -q -x examples/example.gdb --args <tarantool> examples/mixed.lua sort on fiber
# Replace <luajit-src> and <tarantool-src> with the sources of the very
# Tarantool build being debugged (luajit_dbg.py must match its LuaJIT).
set pagination off
set confirm off
handle SIGPROF nostop noprint pass
source <luajit-src>/src/luajit_dbg.py
source lj_bt.py

# Optional, for `tt-fiber <id>` and `lj-bt --all-fibers`; load it after the
# process has started, and import gdb.printing first.
# python import gdb.printing
# source <tarantool-src>/tools/tarantool-gdb.py

break lbox_session_id
run
delete
break box_index_get
continue
echo \n===== bt -no-filters (plain gdb)\n
bt -no-filters
echo \n===== lj-bt\n
lj-bt
echo \n===== bt (lj-bt frame filter)\n
bt
