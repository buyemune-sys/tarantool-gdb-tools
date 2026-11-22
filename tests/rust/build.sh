#!/bin/sh
# Build the Rust modules of the `rust` group of the lj-bt tests:
#
#   tests/rust/build.sh [plain] [tnt]       (default: both)
#
# into $LJBT_RUST_TARGET (default: tests/rust/target):
#
#   <target>/<profile>/libljbt_plain.so   profiles ljbt-debug, release,
#                                         ljbt-strip, ljbt-abort
#   <target>/ljbt-debug/libljbt_tnt.so
#
# Each module is a cargo workspace of its own with its Cargo.lock
# (--locked).  plain needs no crates and builds offline; tnt fetches the
# crates of its lock file unless they are in $CARGO_HOME already.  The
# target directory keeps cargo incremental: a second run is a no-op.
# CARGO names the cargo binary (default: cargo).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
target=${LJBT_RUST_TARGET:-$here/target}
cargo=${CARGO:-cargo}
[ $# -eq 0 ] && set -- plain tnt
for m in "$@"; do
    case $m in
    plain) profiles="ljbt-debug release ljbt-strip ljbt-abort" ;;
    tnt) profiles="ljbt-debug" ;;
    *) echo "build.sh: unknown module: $m" >&2; exit 2 ;;
    esac
    for p in $profiles; do
        "$cargo" build --locked --manifest-path "$here/$m/Cargo.toml" \
            --target-dir "$target" --profile "$p"
    done
done
