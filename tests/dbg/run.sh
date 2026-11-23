#!/bin/sh
# Run the tests of luajit_dbg.py (and, later, tarantool-gdb.py).
#
#   tests/dbg/run.sh [GROUP...] [-- unittest arguments]
#
# Groups:
#   luajit   luajit_dbg.py: every command live and on a core (default)
#   all      every group
#
# Environment (see tests/dbg/harness.py): LJBT_TARANTOOL is required (a
# tarantool binary with DWARF, as tests/fetch.sh prints); DBG_LUAJIT_DBG
# runs the tests against another luajit_dbg.py, e.g. the upstream one,
# to see a test of a fix fail before the fix; DBG_KEEP, DBG_VERBOSE,
# TMPDIR.
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
[ "$groups" = " all" ] && groups=luajit
cd "$here" || exit 1
export PYTHONDONTWRITEBYTECODE=1
rc=0
for g in $groups; do
    case $g in
    luajit) mod=test_luajit ;;
    *) echo "unknown group: $g" >&2; exit 2 ;;
    esac
    echo "### $g"
    python3 -m unittest -v "$@" $mod || rc=1
done
exit $rc
