#!/bin/sh
# Run the tests of luajit_dbg.py and tarantool-gdb.py.
#
#   tests/dbg/run.sh [GROUP...] [-- unittest or pytest arguments]
#
# Groups:
#   luajit    luajit_dbg.py: every command live and on a core (default)
#   upstream  the vendored LuaJIT debugger tests (upstream/luajit), run
#             against tarantool by upstream/run_luajit.py
#   tarantool tarantool-gdb.py: every command and pretty-printer live
#             and on a core
#   upstream-tarantool
#             the vendored tests of tarantool's tools/tarantool-gdb.py
#             (upstream/tarantool), run with pytest by
#             upstream/run_tarantool.py
#   cores     the thread stacks in cores of tarantool: gcore, gcore-pages
#             (gcore_pages.py), the kernel's core (test_cores.py)
#   all       every group
#
# Environment (see tests/dbg/harness.py): LJBT_TARANTOOL is required (a
# tarantool binary with DWARF, as tests/fetch.sh prints); DBG_LUAJIT_DBG
# and DBG_TT_GDB run the tests against another luajit_dbg.py or
# tarantool-gdb.py, e.g. the upstream one, to see a test of a fix fail
# before the fix; DBG_GCORE_PAGES, DBG_GDB, DBG_KEEP, DBG_VERBOSE,
# DBG_UPSTREAM_TIMEOUT, DBG_PYTHON (a python3 with pytest for
# upstream-tarantool), DBG_CORES_SUMMARY (test_cores.py), TMPDIR.
# DBG_DEBUGGER=lldb runs the luajit and upstream groups in lldb
# (DBG_LLDB: the lldb binary; gdb is still needed, see harness.py).
#
# A single test: tests/dbg/run.sh luajit -- -k TestStack
set -u
here=$(cd "$(dirname "$0")" && pwd)
: "${LJBT_TARANTOOL:?set LJBT_TARANTOOL to a tarantool binary with DWARF}"
groups=
while [ $# -gt 0 ]; do
    case $1 in
    --) shift; break ;;
    *) groups="$groups $1"; shift ;;
    esac
done
[ -z "$groups" ] && groups=luajit
[ "$groups" = " all" ] &&
    groups="luajit upstream tarantool upstream-tarantool cores"
cd "$here" || exit 1
export PYTHONDONTWRITEBYTECODE=1
rc=0
for g in $groups; do
    echo "### $g"
    case $g in
    luajit) python3 -m unittest -v "$@" test_luajit || rc=1 ;;
    upstream) python3 upstream/run_luajit.py "$@" || rc=1 ;;
    tarantool) python3 -m unittest -v "$@" test_tarantool || rc=1 ;;
    upstream-tarantool) python3 upstream/run_tarantool.py "$@" || rc=1 ;;
    cores) python3 -m unittest -v "$@" test_cores || rc=1 ;;
    *) echo "unknown group: $g" >&2; exit 2 ;;
    esac
done
exit $rc
