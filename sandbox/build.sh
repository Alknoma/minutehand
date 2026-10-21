#!/bin/sh
# Builds the gVisor sandbox whose clock Minutehand owns: `runsc` with `gvisor-sim-clock.patch` applied, and the
# sidecars it loads from `gvisor-bin/` beside it, from the `go` branch of gVisor at the commit the patch was written
# against. Needs Docker (the build runs in golang:1.27, for the machine's own architecture) and git.
#
#   sandbox/build.sh [out_dir]      -> out_dir/runsc, out_dir/gvisor-bin/*   (default: sandbox/dist)
#
# The C sidecars are built as gVisor's BUILD files on the matching master commit build them: static, freestanding,
# with no C library (each carries its own `_start`).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "${1:-$HERE/dist}" 2>/dev/null && pwd || { mkdir -p "${1:-$HERE/dist}" && cd "${1:-$HERE/dist}" && pwd; })
COMMIT=c324264fbb547ad60710ee40d47fa95faed3b75d   # google/gvisor, branch `go`, "Merge release-20261012.0-262"
SRC="$HERE/.gvisor-go"

if [ ! -d "$SRC/.git" ]; then
  git init -q "$SRC"
  git -C "$SRC" remote add origin https://github.com/google/gvisor
fi
git -C "$SRC" fetch -q --depth 1 origin "$COMMIT"
git -C "$SRC" checkout -q --force "$COMMIT"
git -C "$SRC" clean -qfd
git -C "$SRC" apply "$HERE/gvisor-sim-clock.patch"

docker run --rm -v "$SRC:/src" -w /src -v minutehand-gvisor-gomod:/go/pkg/mod -v minutehand-gvisor-gocache:/root/.cache/go-build \
  -e CGO_ENABLED=0 golang:1.27 sh -eu -c '
    mkdir -p /src/out/gvisor-bin
    go build -buildvcs=false -o /src/out/runsc ./runsc
    go build -buildvcs=false -o /src/out/gvisor-bin/gvisor_sentry ./runsc/cmd/sentry
    go build -buildvcs=false -o /src/out/gvisor-bin/checkpointgofer ./runsc/checkpointgofer
    C="gcc -O2 -static -nostdlib -nostartfiles -ffreestanding -fno-stack-protector -fno-builtin -fPIE -pie -Wl,--build-id=none"
    $C -o /src/out/gvisor-bin/gvisor-sentry-prewarmer runsc/prewarmer/prewarmer.c
    $C -o /src/out/gvisor-bin/runsc-fd-parking runsc/fdparking/fdparking.c
    $C -o /src/out/gvisor-bin/gvisor-proc-umounter runsc/procumounter/procumounter.c
  '
mkdir -p "$OUT/gvisor-bin"
cp "$SRC/out/runsc" "$OUT/runsc"
cp "$SRC"/out/gvisor-bin/* "$OUT/gvisor-bin/"
chmod 0755 "$OUT/runsc" "$OUT"/gvisor-bin/*
echo "built: $OUT/runsc and $(ls "$OUT/gvisor-bin" | wc -l | tr -d ' ') sidecars in $OUT/gvisor-bin"
