#!/bin/bash
set -euo pipefail

# ISP selfpath provides a hardware-scaled 1080p frame. This keeps browser
# decoding smooth while the tighter encoder settings preserve document text.
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
OCR_FRAME_PATTERN="${CAMERA_OCR_FRAME_PATTERN:-/tmp/rk3588_camera_ocr_%08d.jpg}"
OCR_SNAPSHOT_FPS="${CAMERA_OCR_SNAPSHOT_FPS:-5}"
OCR_JPEG_QUALITY="${CAMERA_OCR_JPEG_QUALITY:-90}"

restore_camera_path() {
  # Both camera paths return to the full IMX415 sensor field on shutdown.
  if [[ "$VIDEO_DEVICE" == "/dev/video23" ]]; then
    v4l2-ctl -d "$VIDEO_DEVICE" --set-fmt-video=width=1920,height=2160,pixelformat=NV12 >/dev/null 2>&1 || true
  else
    v4l2-ctl -d "$VIDEO_DEVICE" --set-fmt-video=width=3840,height=2160,pixelformat=NV12 >/dev/null 2>&1 || true
  fi
  v4l2-ctl -d "$VIDEO_DEVICE" --set-selection=target=crop,left=0,top=0,width=3840,height=2160 >/dev/null 2>&1 || true
}
trap restore_camera_path EXIT INT TERM

echo "CSI camera WebRTC stream: $VIDEO_DEVICE -> ${WIDTH}x${HEIGHT}@${FPS}, H.264 ${TARGET_BITRATE}bps, GOP ${GOP_SIZE}"

gst-launch-1.0 -q -e \
  v4l2src "device=$VIDEO_DEVICE" io-mode=2 do-timestamp=true ! \
  "video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT,framerate=$FPS/1" ! \
  tee name=source \
  source. ! \
  queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! \
  mpph264enc "bps=$TARGET_BITRATE" "bps-min=$MIN_BITRATE" "bps-max=$MAX_BITRATE" \
    "gop=$GOP_SIZE" profile=100 "level=$H264_LEVEL" header-mode=1 rc-mode=0 "max-reenc=$MAX_REENC" \
    "qp-init=$QP_INIT" "qp-min=$QP_MIN" "qp-max=$QP_MAX" "qp-min-i=$QP_MIN" "qp-max-i=$QP_MAX" \
    qos=true zero-copy-pkt=false ! \
  h264parse config-interval=-1 ! \
  rtph264pay pt=96 config-interval=-1 aggregate-mode=zero-latency mtu=1200 ! \
  udpsink host=127.0.0.1 "port=$RTP_PORT" buffer-size=4194304 sync=false async=false \
  source. ! \
  queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! \
  videorate drop-only=true "max-rate=$OCR_SNAPSHOT_FPS" ! \
  "video/x-raw,format=NV12,width=$WIDTH,height=$HEIGHT,framerate=$OCR_SNAPSHOT_FPS/1" ! \
  videoconvert n-threads=4 ! \
  jpegenc "quality=$OCR_JPEG_QUALITY" ! \
  multifilesink "location=$OCR_FRAME_PATTERN" max-files=3 sync=false async=false
