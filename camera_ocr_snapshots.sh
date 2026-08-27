#!/bin/bash
set -euo pipefail

# The ISP mainpath keeps the sensor's native detail for OCR while the browser
# preview remains on the independent 1080p selfpath.
VIDEO_DEVICE="${CAMERA_OCR_VIDEO_DEVICE:-/dev/video22}"
WIDTH="${CAMERA_OCR_WIDTH:-3840}"
HEIGHT="${CAMERA_OCR_HEIGHT:-2160}"
SOURCE_FPS="${CAMERA_OCR_SOURCE_FPS:-30}"
SNAPSHOT_FPS="${CAMERA_OCR_SNAPSHOT_FPS:-5}"
JPEG_QUALITY="${CAMERA_OCR_JPEG_QUALITY:-95}"
FRAME_PATTERN="${CAMERA_OCR_FRAME_PATTERN:-/tmp/rk3588_camera_ocr_%08d.jpg}"
MAX_FILES="${CAMERA_OCR_MAX_FILES:-3}"

restore_camera_path() {
  v4l2-ctl -d "$VIDEO_DEVICE" \
    --set-fmt-video=width=3840,height=2160,pixelformat=NV12 >/dev/null 2>&1 || true
  v4l2-ctl -d "$VIDEO_DEVICE" \
    --set-selection=target=crop,left=0,top=0,width=3840,height=2160 >/dev/null 2>&1 || true
}
trap restore_camera_path EXIT INT TERM

echo "CSI camera OCR snapshots: $VIDEO_DEVICE -> ${WIDTH}x${HEIGHT}@${SNAPSHOT_FPS}, JPEG quality ${JPEG_QUALITY}"

gst-launch-1.0 -q -e \
  v4l2src "device=$VIDEO_DEVICE" io-mode=2 do-timestamp=true ! \
  "video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT,framerate=$SOURCE_FPS/1" ! \
  queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! \
  videorate drop-only=true "max-rate=$SNAPSHOT_FPS" ! \
  "video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT,framerate=$SNAPSHOT_FPS/1" ! \
  videoconvert n-threads=4 ! \
  jpegenc "quality=$JPEG_QUALITY" ! \
  multifilesink "location=$FRAME_PATTERN" "max-files=$MAX_FILES" sync=false async=false
