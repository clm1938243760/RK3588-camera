#!/bin/bash
set -euo pipefail

# ISP selfpath provides a hardware-scaled 1080p frame for browser preview.
# v4l2src resets the RKISP crop to the 1920x1080 output size, which shows only
# part of the 4K sensor. v4l2-ctl preserves the required ioctl order: set the
# 1080p output format, select the full sensor crop, then start streaming.
VIDEO_DEVICE="${CAMERA_VIDEO_DEVICE:-/dev/video23}"
RTP_PORT="${CAMERA_RTP_PORT:-5006}"
WIDTH="${CAMERA_WIDTH:-1920}"
HEIGHT="${CAMERA_HEIGHT:-1080}"
FPS="${CAMERA_FPS:-30}"
TARGET_BITRATE="${CAMERA_BITRATE:-12000000}"
MIN_BITRATE="${CAMERA_MIN_BITRATE:-6000000}"
MAX_BITRATE="${CAMERA_MAX_BITRATE:-20000000}"
GOP_SIZE="${CAMERA_GOP_SIZE:-30}"
H264_LEVEL="${CAMERA_H264_LEVEL:-40}"
QP_MIN="${CAMERA_QP_MIN:-12}"
QP_MAX="${CAMERA_QP_MAX:-26}"
QP_INIT="${CAMERA_QP_INIT:-20}"
MAX_REENC="${CAMERA_MAX_REENC:-2}"
SENSOR_WIDTH="${CAMERA_SENSOR_WIDTH:-3840}"
SENSOR_HEIGHT="${CAMERA_SENSOR_HEIGHT:-2160}"

if (( WIDTH <= 0 || HEIGHT <= 0 || WIDTH % 2 || HEIGHT % 2 )); then
  echo "CAMERA_WIDTH and CAMERA_HEIGHT must be positive even values" >&2
  exit 2
fi
RAW_FRAME_BYTES=$((WIDTH * HEIGHT * 3 / 2))
STOP_REQUESTED=0

restore_camera_path() {
  # Both camera paths return to the full IMX415 sensor field on shutdown.
  if [[ "$VIDEO_DEVICE" == "/dev/video23" ]]; then
    v4l2-ctl -d "$VIDEO_DEVICE" --set-fmt-video=width=1920,height=2160,pixelformat=NV12 >/dev/null 2>&1 || true
  else
    v4l2-ctl -d "$VIDEO_DEVICE" --set-fmt-video=width=3840,height=2160,pixelformat=NV12 >/dev/null 2>&1 || true
  fi
  v4l2-ctl -d "$VIDEO_DEVICE" --set-selection=target=crop,left=0,top=0,width=3840,height=2160 >/dev/null 2>&1 || true
}
request_stop() {
  STOP_REQUESTED=1
}
trap restore_camera_path EXIT
trap request_stop INT TERM

echo "CSI camera WebRTC stream: $VIDEO_DEVICE -> ${WIDTH}x${HEIGHT}@${FPS}, H.264 ${TARGET_BITRATE}bps, GOP ${GOP_SIZE}"

v4l2-ctl -d "$VIDEO_DEVICE" \
  --set-fmt-video="width=$WIDTH,height=$HEIGHT,pixelformat=NV12"
v4l2-ctl -d "$VIDEO_DEVICE" \
  --set-selection="target=crop,left=0,top=0,width=$SENSOR_WIDTH,height=$SENSOR_HEIGHT"

set +e
v4l2-ctl --silent -d "$VIDEO_DEVICE" \
  --stream-mmap=3 --stream-to=- 2>/dev/null | \
gst-launch-1.0 -q -e \
  fdsrc fd=0 do-timestamp=true "blocksize=$RAW_FRAME_BYTES" ! \
  rawvideoparse format=nv12 "width=$WIDTH" "height=$HEIGHT" "framerate=$FPS/1" ! \
  queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! \
  mpph264enc "bps=$TARGET_BITRATE" "bps-min=$MIN_BITRATE" "bps-max=$MAX_BITRATE" \
    "gop=$GOP_SIZE" profile=100 "level=$H264_LEVEL" header-mode=1 rc-mode=0 "max-reenc=$MAX_REENC" \
    "qp-init=$QP_INIT" "qp-min=$QP_MIN" "qp-max=$QP_MAX" "qp-min-i=$QP_MIN" "qp-max-i=$QP_MAX" \
    qos=true zero-copy-pkt=false ! \
  h264parse config-interval=-1 ! \
  rtph264pay pt=96 config-interval=-1 aggregate-mode=zero-latency mtu=1200 ! \
  udpsink host=127.0.0.1 "port=$RTP_PORT" buffer-size=4194304 sync=false async=false
PIPELINE_STATUS=("${PIPESTATUS[@]}")
set -e

if (( STOP_REQUESTED )); then
  exit 0
fi
if (( PIPELINE_STATUS[0] != 0 )); then
  echo "V4L2 preview capture exited with status ${PIPELINE_STATUS[0]}" >&2
  exit "${PIPELINE_STATUS[0]}"
fi
if (( PIPELINE_STATUS[1] != 0 )); then
  echo "WebRTC encoder pipeline exited with status ${PIPELINE_STATUS[1]}" >&2
  exit "${PIPELINE_STATUS[1]}"
fi
