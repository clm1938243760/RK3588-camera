#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ZOO_ROOT=${MODEL_ZOO_ROOT:-/userdata/aidemo/rknn_model_zoo_ppocr_min}
BUILD_ROOT=${BUILD_ROOT:-/var/tmp/rk3588-ppocr-parallel-worker-build}
PRODUCTION_WORKER=/userdata/aidemo/rknn_PPOCR-System_demo_native/rknn_ppocr_system_worker

usage() {
  cat <<EOF
Usage: sudo $0 /path/to/candidate-worker

Builds the three-NPU-core PP-OCR line-recognition worker without replacing the
active production worker. MODEL_ZOO_ROOT may override the RKNN Model Zoo source
tree. The output path must differ from:
  $PRODUCTION_WORKER
EOF
}

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 2
fi
if [[ $(uname -m) != aarch64 ]]; then
  echo "This worker must be built on ARM64/aarch64." >&2
  exit 1
fi

OUTPUT=$(realpath -m "$1")
if [[ $OUTPUT == "$PRODUCTION_WORKER" ]]; then
  echo "Refusing to overwrite the active production worker." >&2
  exit 1
fi
case "$BUILD_ROOT" in
  /var/tmp/rk3588-ppocr-*-build) ;;
  *)
    echo "Refusing unsafe BUILD_ROOT: $BUILD_ROOT" >&2
    exit 1
    ;;
esac

SOURCE_BASE="$MODEL_ZOO_ROOT/examples/PPOCR/PPOCR-System/cpp"
for path in \
  "$SOURCE_BASE/postprocess.cc" \
  "$SOURCE_BASE/clipper.cc" \
  "$SOURCE_BASE/clipper.h" \
  "$SOURCE_BASE/dict.h" \
  "$SOURCE_BASE/ppocr_system.h" \
  "$ROOT/scripts/ppocr_rknn_worker.cc" \
  "$ROOT/scripts/ppocr_system_parallel.cc" \
  "$ROOT/scripts/ppocr_parallel_worker.CMakeLists.txt"; do
  if [[ ! -f $path ]]; then
    echo "Required source file is missing: $path" >&2
    exit 1
  fi
done

rm -rf -- "$BUILD_ROOT"
install -d -m 0755 "$BUILD_ROOT/source" "$BUILD_ROOT/build" "$(dirname "$OUTPUT")"
trap 'rm -rf -- "$BUILD_ROOT"' EXIT

install -m 0644 "$ROOT/scripts/ppocr_parallel_worker.CMakeLists.txt" "$BUILD_ROOT/source/CMakeLists.txt"
install -m 0644 "$ROOT/scripts/ppocr_rknn_worker.cc" "$BUILD_ROOT/source/ppocr_rknn_worker.cc"
install -m 0644 "$ROOT/scripts/ppocr_system_parallel.cc" "$BUILD_ROOT/source/ppocr_system.cc"
for name in postprocess.cc clipper.cc clipper.h dict.h ppocr_system.h; do
  install -m 0644 "$SOURCE_BASE/$name" "$BUILD_ROOT/source/$name"
done

cmake \
  -S "$BUILD_ROOT/source" \
  -B "$BUILD_ROOT/build" \
  -DTARGET_SOC=rk3588 \
  -DCMAKE_BUILD_TYPE=Release \
  -DMODEL_ZOO_ROOT="$MODEL_ZOO_ROOT"
cmake --build "$BUILD_ROOT/build" --target rknn_ppocr_system_parallel_worker -j2
install -m 0755 "$BUILD_ROOT/build/rknn_ppocr_system_parallel_worker" "$OUTPUT"
sha256sum "$OUTPUT"
