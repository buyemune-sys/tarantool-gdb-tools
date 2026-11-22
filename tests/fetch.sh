#!/bin/sh
# Fetch an official Tarantool build with DWARF and the gdb extensions of
# its sources, for the tests:
#
#   tests/fetch.sh <version> <amd64|arm64> <dest>
#
# The binary is the static deb of Tarantool's package repository (it
# carries .debug_info).  luajit_dbg.py (luajit-gdb.py before LuaJIT
# 5db8c217) comes from tarantool/luajit at the commit of the
# third_party/luajit submodule of the tag, tools/tarantool-gdb.py from
# the tag.  Prints the three variables for the tests, ready for
# `export` or $GITHUB_ENV:
#
#   LJBT_TARANTOOL=<dest>/usr/bin/tarantool
#   LJBT_LUAJIT_DBG=<dest>/luajit_dbg.py
#   LJBT_TT_GDB=<dest>/tarantool-gdb.py
#
# Already fetched files are kept.  GITHUB_TOKEN, if set, authorizes the
# GitHub API call; LJBT_TARANTOOL_GIT and LJBT_LUAJIT_GIT, if set, are
# local clones of tarantool/tarantool and tarantool/luajit used instead
# of the network for the sources.
set -eu

[ $# -eq 3 ] || { echo "usage: $0 <version> <amd64|arm64> <dest>" >&2; exit 2; }
ver=$1 arch=$2 dest=$3
deb_url="https://hb.bizmrg.com/tarantool_repo/release/series-3/linux-deb/pool/static/main/t/tarantool/tarantool_${ver}-1_${arch}.deb"
raw=https://raw.githubusercontent.com

log() { echo "fetch.sh: $*" >&2; }

mkdir -p "$dest"
bin="$dest/usr/bin/tarantool"
if [ ! -x "$bin" ]; then
    log "downloading $deb_url"
    curl -fsSL --retry 3 -o "$dest/tarantool.deb" "$deb_url"
    dpkg-deb -x "$dest/tarantool.deb" "$dest"
    rm -f "$dest/tarantool.deb"
fi
if ! readelf -S "$bin" | grep -q '\.debug_info'; then
    log "$bin has no .debug_info"
    exit 1
fi

# The LuaJIT of the tag.
if [ ! -s "$dest/luajit.commit" ]; then
    if [ -n "${LJBT_TARANTOOL_GIT:-}" ]; then
        git -C "$LJBT_TARANTOOL_GIT" ls-tree "$ver" third_party/luajit |
            awk '{print $3}' > "$dest/luajit.commit"
    else
        curl -fsSL --retry 3 \
            ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} \
            "https://api.github.com/repos/tarantool/tarantool/contents/third_party/luajit?ref=$ver" |
            python3 -c 'import json, sys; print(json.load(sys.stdin)["sha"])' \
            > "$dest/luajit.commit"
    fi
fi
lj=$(cat "$dest/luajit.commit")
[ -n "$lj" ] || { log "no LuaJIT commit for $ver"; exit 1; }

get_luajit() {         # <file> -> $dest/<file>, false if there is none
    if [ -n "${LJBT_LUAJIT_GIT:-}" ]; then
        git -C "$LJBT_LUAJIT_GIT" show "$lj:src/$1" > "$dest/$1" 2>/dev/null
    else
        # Quiet: a 404 for luajit_dbg.py just means an older LuaJIT.
        curl -fsL --retry 3 -o "$dest/$1" "$raw/tarantool/luajit/$lj/src/$1"
    fi || { rm -f "$dest/$1"; return 1; }
}

ext=
for f in luajit_dbg.py luajit-gdb.py; do
    if [ -s "$dest/$f" ] || get_luajit "$f"; then
        ext="$dest/$f"
        break
    fi
done
[ -n "$ext" ] || { log "no gdb extension in tarantool/luajit at $lj"; exit 1; }

if [ ! -s "$dest/tarantool-gdb.py" ]; then
    if [ -n "${LJBT_TARANTOOL_GIT:-}" ]; then
        git -C "$LJBT_TARANTOOL_GIT" show "$ver:tools/tarantool-gdb.py" \
            > "$dest/tarantool-gdb.py"
    else
        curl -fsSL --retry 3 -o "$dest/tarantool-gdb.py" \
            "$raw/tarantool/tarantool/$ver/tools/tarantool-gdb.py"
    fi
fi

log "$ver $arch: LuaJIT $lj, $(basename "$ext")"
echo "LJBT_TARANTOOL=$bin"
echo "LJBT_LUAJIT_DBG=$ext"
echo "LJBT_TT_GDB=$dest/tarantool-gdb.py"
