#!/bin/sh
# Run the lj-bt tests.
#
#   tests/run.sh [GROUP...]
#
# Groups:
#   oracle   layer 1: fixed chains against debug.getinfo
#   random   layer 2: random chains against the generator's model
#   stops    layer 3: random stops (SIGPROF) on a mixed load
#   core     layer 4: post-mortem on gcore files
#   tail     layer 5: the tail call heuristic table
#   crash    layer 5: the sysprof mode C crashes (long, racy)
#   all      everything; default: oracle core random stops tail
#
# Environment (see tests/common.py): LJBT_TARANTOOL and LJBT_LUAJIT_DBG are
# required; LJBT_TT_GDB, LJBT_SEEDS, LJBT_SEED0, LJBT_STOPS, LJBT_JIT,
# LJBT_ATTEMPTS, LJBT_CRASH_SECS, LJBT_KEEP, LJBT_VERBOSE; TMPDIR.
set -u
here=$(cd "$(dirname "$0")" && pwd)
: "${LJBT_TARANTOOL:?set LJBT_TARANTOOL to a tarantool binary with DWARF}"
: "${LJBT_LUAJIT_DBG:?set LJBT_LUAJIT_DBG to luajit_dbg.py of that build}"
[ $# -eq 0 ] && set -- oracle core random stops tail
[ "$*" = all ] && set -- oracle core random stops tail crash
cd "$here" || exit 1
rc=0
for g in "$@"; do
    case $g in
    oracle) set_args="test_oracle" ;;
    random) set_args="test_random" ;;
    stops) set_args="test_stops" ;;
    core) set_args="test_core" ;;
    tail) set_args="test_regress -k Tail" ;;
    crash) set_args="test_regress -k Crash" ;;
    *) echo "unknown group: $g" >&2; exit 2 ;;
    esac
    echo "### $g"
    # shellcheck disable=SC2086
    python3 -m unittest -v $set_args || rc=1
done
exit $rc
