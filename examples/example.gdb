# lj-bt on examples/mixed.lua.  From the repository root:
#
#   gdb -x examples/example.gdb --args tarantool examples/mixed.lua sort on main
#
# Stops in the C implementation of box.space.t:get() called from a
# table.sort comparator and prints bt, lj-bt and the stitched bt.
set pagination off
set confirm off
set print frame-arguments none
handle SIGPROF nostop noprint pass
handle SIGPIPE nostop noprint pass
source luajit_gdb.py
source luajit_backtrace_gdb.py
break lbox_session_id
run
delete
break box_index_get
continue
echo \n===== bt -no-filters\n
bt -no-filters
echo \n===== lj-bt\n
lj-bt
echo \n===== bt (lj-bt frame filter)\n
bt
echo \n===== lj-bt --tail-chain -v\n
lj-bt --tail-chain -v
