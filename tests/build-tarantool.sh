#!/bin/sh
# Build Tarantool from source as a dynamic PIE with DWARF, for the tests:
#
#   tests/build-tarantool.sh <version> <gc64|nogc64> <dest>
#
# The official packages of 3.x are static non-PIE builds, a self-built
# tarantool is usually a dynamic PIE: gdb relocates it when the program
# starts, the extensions must not keep an address from before.  This
# clones the tag <version> of tarantool/tarantool with its submodules,
# builds it RelWithDebInfo out of tree with LUAJIT_ENABLE_GC64 ON
# (gc64) or OFF (nogc64), and copies to <dest> the binary, the gdb
# extension of its LuaJIT (luajit_dbg.py, luajit-gdb.py before LuaJIT
# 5db8c217) and its tools/tarantool-gdb.py.  The binary is a PIE because
# the compiler of the distribution makes one by default (Ubuntu); it is
# checked to be one, with .debug_info and the GC64 mode asked for.
# Prints the variables for the tests, as tests/fetch.sh does:
#
#   LJBT_TARANTOOL=<dest>/bin/tarantool
#   LJBT_LUAJIT_DBG=<dest>/luajit_dbg.py
#   LJBT_TT_GDB=<dest>/tarantool-gdb.py
#
# The binary is in bin/: next to it, tarantool-gdb.py would be gdb's
# auto-load script of the objfile (declined, with a warning, by the
# default safe-path).  An existing binary is kept (a cache): nothing is
# cloned or built, the binary is only checked.  The sources and the
# build tree go to $LJBT_BUILD_DIR (default: <dest>.build), removed
# after the build unless LJBT_KEEP_BUILD is set.  LJBT_TARANTOOL_GIT is
# the repository to clone (default:
# https://github.com/tarantool/tarantool.git); CMAKE_EXTRA_ARGS, more
# arguments of cmake (word-split), e.g. the headers of a library whose
# -dev package is not installed.  3.9.0 runs autoreconf for its bundled
# libunwind; master takes a release tarball of it instead.
#
# Build dependencies on Ubuntu (tarantool's own CI and docs): a C/C++
# toolchain, cmake, autoconf, automake, libtool (the bundled libunwind),
# libreadline-dev, libncurses-dev, libicu-dev, libssl-dev, zlib1g-dev.
set -eu

[ $# -eq 3 ] || {
    echo "usage: $0 <version> <gc64|nogc64> <dest>" >&2
    exit 2
}
ver=$1 variant=$2 dest=$3
case $variant in
gc64) gc64=ON want=true ;;
nogc64) gc64=OFF want=false ;;
*) echo "unknown variant: $variant (gc64 or nogc64)" >&2; exit 2 ;;
esac
git_url=${LJBT_TARANTOOL_GIT:-https://github.com/tarantool/tarantool.git}
work=${LJBT_BUILD_DIR:-$dest.build}

log() { echo "build-tarantool.sh: $*" >&2; }

mkdir -p "$dest/bin"
dest=$(cd "$dest" && pwd)
bin="$dest/bin/tarantool"
if [ ! -x "$bin" ]; then
    rm -rf "$work"
    mkdir -p "$work"
    src="$work/src" build="$work/build"
    log "cloning $ver of $git_url"
    # A shallow clone of the tag still has it: the version comes from
    # `git describe` (CMakeLists.txt).
    git clone --quiet --depth 1 --branch "$ver" --recurse-submodules \
        --shallow-submodules "$git_url" "$src"
    # LuaJIT takes its version from `git describe` in its submodule
    # (cmake/SetVersion.cmake): that needs its history back to a tag.
    git -C "$src/third_party/luajit" fetch --quiet --unshallow --tags
    log "building $ver, LUAJIT_ENABLE_GC64=$gc64"
    # shellcheck disable=SC2086
    cmake -S "$src" -B "$build" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DLUAJIT_ENABLE_GC64="$gc64" \
        -DENABLE_BACKTRACE=ON \
        -DENABLE_FEEDBACK_DAEMON=OFF \
        ${CMAKE_EXTRA_ARGS:-} > "$work/cmake.log" 2>&1 ||
        { tail -50 "$work/cmake.log" >&2; exit 1; }
    cmake --build "$build" --target tarantool --parallel "$(nproc)" \
        > "$work/build.log" 2>&1 ||
        { tail -50 "$work/build.log" >&2; exit 1; }
    ext=
    for f in luajit_dbg.py luajit-gdb.py; do
        if [ -s "$src/third_party/luajit/src/$f" ]; then
            ext=$f
            break
        fi
    done
    [ -n "$ext" ] || { log "no gdb extension in $src/third_party/luajit/src"; exit 1; }
    cp "$src/third_party/luajit/src/$ext" "$src/tools/tarantool-gdb.py" \
        "$dest/"
    # Last: a binary in dest means a complete build.
    cp "$build/src/tarantool" "$bin.tmp"
    mv "$bin.tmp" "$bin"
    [ -n "${LJBT_KEEP_BUILD:-}" ] || rm -rf "$work"
fi

fail() { log "$bin: $*"; exit 1; }
readelf -h "$bin" | grep -q 'Type: *DYN' || fail "not a PIE"
readelf -l "$bin" | grep -q 'INTERP' || fail "not dynamically linked"
readelf -S "$bin" | grep -q '\.debug_info' || fail "no .debug_info"
got=$("$bin" -e "print(require('ffi').abi('gc64')) os.exit(0)") ||
    fail "does not run"
[ "$got" = "$want" ] || fail "ffi.abi('gc64') is $got, not $want"
ext=
for f in luajit_dbg.py luajit-gdb.py; do
    [ -s "$dest/$f" ] && { ext="$dest/$f"; break; }
done
[ -n "$ext" ] || fail "no gdb extension of its LuaJIT in $dest"

log "$("$bin" --version | head -1), $variant, PIE, $(basename "$ext")"
echo "LJBT_TARANTOOL=$bin"
echo "LJBT_LUAJIT_DBG=$ext"
echo "LJBT_TT_GDB=$dest/tarantool-gdb.py"
