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
#   cli      lj-bt through gdb commands: flags, threads, fibers, limits
#   env      surroundings: missing extension, no DWARF, symbol-file, example
#   rust     Lua and Rust modules (tests/rust) calling each other; builds
#            them with cargo, skipped without it
#   window   every instruction of the VM's windows of chain steps (stepi),
#            and SIGPROF stops of setitimer without sysprof
#   all      everything; default: oracle core random stops tail cli env
#            window rust
#
# LJBT_LJ_BT runs the tests against another lj_bt.py (an older one, to see
# a new test fail before its fix).
#
# Environment (see tests/common.py): LJBT_TARANTOOL and LJBT_LUAJIT_DBG are
# required; LJBT_TT_GDB, LJBT_SEEDS, LJBT_SEED0, LJBT_STOPS, LJBT_JIT,
# LJBT_ATTEMPTS, LJBT_CRASH_SECS, LJBT_KEEP, LJBT_VERBOSE; TMPDIR;
# LJBT_RUST_TARGET, CARGO (tests/test_rust.py).
set -u
here=$(cd "$(dirname "$0")" && pwd)
: "${LJBT_TARANTOOL:?set LJBT_TARANTOOL to a tarantool binary with DWARF}"
: "${LJBT_LUAJIT_DBG:?set LJBT_LUAJIT_DBG to luajit_dbg.py of that build}"
[ $# -eq 0 ] && set -- oracle core random stops tail cli env window rust
[ "$*" = all ] && set -- oracle core random stops tail cli env window rust \
    crash
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
    cli) set_args="test_cli" ;;
    env) set_args="test_env" ;;
    rust) set_args="test_rust" ;;
    window) set_args="test_window" ;;
    *) echo "unknown group: $g" >&2; exit 2 ;;
    esac
    echo "### $g"
    # shellcheck disable=SC2086
    python3 -m unittest -v $set_args || rc=1
done
exit $rc
