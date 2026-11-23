#!/bin/sh
# Re-sync the vendored upstream tests:
#
#   tests/dbg/upstream/sync.sh luajit|tarantool|tarantool-mp [<ref>] [--write]
#
# fetches the files from <ref> of the upstream repository (a branch, a
# tag or a commit; the default is the one in README.md) and shows the
# diff against the vendored copies; --write replaces them.  Then put the
# commit in README.md.
#
#   luajit        tarantool/luajit test/tarantool-debugger-tests/
#   tarantool     tarantool/tarantool test/gdb/ (PR #13078)
#   tarantool-mp  tarantool/tarantool tools/test-tarantool-gdb.py (PR #9665)
#
# DBG_LUAJIT_GIT and DBG_TARANTOOL_GIT, if set, are local clones of
# tarantool/luajit and tarantool/tarantool used instead of the network
# (`git show <ref>:<path>`; fetch the ref into it first, e.g.
# `git fetch origin pull/13078/head`).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 1 ] || { echo "usage: $0 luajit|tarantool|tarantool-mp [<ref>] [--write]" >&2; exit 2; }
what=$1
shift
ref= write=
for a in "$@"; do
    case $a in
    --write) write=1 ;;
    *) ref=$a ;;
    esac
done
case $what in
luajit)
    repo=tarantool/luajit
    git=${DBG_LUAJIT_GIT:-}
    : "${ref:=elhimov/gh-13094-dbg-avoid-hardcoded-enums}"
    # upstream path -> vendored path
    files="test/tarantool-debugger-tests/debug-extension-tests.py=luajit/debug-extension-tests.py"
    ;;
tarantool)
    repo=tarantool/tarantool
    git=${DBG_TARANTOOL_GIT:-}
    # The head of PR #13078 (elhimov/gh-13076-add-fiber-switch-tests).
    : "${ref:=fa8bd1760306454649f128e261a37bfa48fc53b8}"
    files=
    for f in conftest.py test_gdb.py utils.py ut_tt_fiber.py ut_tl_var.py; do
        files="$files test/gdb/$f=tarantool/$f"
    done
    ;;
tarantool-mp)
    repo=tarantool/tarantool
    git=${DBG_TARANTOOL_GIT:-}
    # The head of PR #9665 (the MsgPack refactor).
    : "${ref:=4f7e2fff955286f0373c5e4ecba86985fec78d38}"
    files="tools/test-tarantool-gdb.py=tarantool/test-tarantool-gdb.py"
    ;;
*)
    echo "unknown upstream: $what" >&2
    exit 2
    ;;
esac
tmp=$(mktemp -d "${TMPDIR:-$HOME/.cache}/dbg-sync.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
rc=0
for f in $files; do
    src=${f%%=*} dst=$here/${f#*=}
    new=$tmp/$(basename "$dst")
    if [ -n "$git" ]; then
        git -C "$git" show "$ref:$src" > "$new"
    else
        curl -fsSL --retry 3 -o "$new" \
            "https://raw.githubusercontent.com/$repo/$ref/$src"
    fi
    if diff -u "$dst" "$new"; then
        echo "sync.sh: $dst is up to date with $repo $ref"
    else
        rc=1
        if [ -n "$write" ]; then
            mkdir -p "$(dirname "$dst")"
            cp "$new" "$dst"
            echo "sync.sh: $dst updated from $repo $ref"
        fi
    fi
done
[ -n "$write" ] && rc=0
exit $rc
