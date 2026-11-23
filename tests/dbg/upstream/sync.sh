#!/bin/sh
# Re-sync the vendored upstream tests:
#
#   tests/dbg/upstream/sync.sh luajit [<ref>] [--write]
#
# fetches the files from <ref> of the upstream repository (a branch, a
# tag or a commit; the default is the one in README.md) and shows the
# diff against the vendored copies; --write replaces them.  Then put the
# commit in README.md.  DBG_LUAJIT_GIT, if set, is a local clone of
# tarantool/luajit used instead of the network (`git show <ref>:<path>`;
# fetch the ref into it first).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 1 ] || { echo "usage: $0 luajit [<ref>] [--write]" >&2; exit 2; }
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
            cp "$new" "$dst"
            echo "sync.sh: $dst updated from $repo $ref"
        fi
    fi
done
[ -n "$write" ] && rc=0
exit $rc
