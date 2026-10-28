#!/usr/bin/env bash
# Build the native arm64 ORFS image inside the Colima (aarch64) VM. Usage: docker/arm64/build.sh [threads]
set -euo pipefail
cd "$(dirname "$0")"
THREADS="${1:-4}"   # 16 GB VM: keep <= 5 (slang/OpenROAD translation units are memory hungry)
docker build --platform linux/arm64 --build-arg numThreads="$THREADS" -t ppa-orfs:arm64 . 2>&1 | tee build.log
