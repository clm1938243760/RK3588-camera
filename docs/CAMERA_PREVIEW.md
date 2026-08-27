# CSI Camera Preview and OCR Monitor

## Data Paths

```text
IMX415 -> /dev/video23 ISP selfpath, full 3840x2160 sensor crop
      -> hardware-scaled 1920x1080 NV12
      -> MPP H.264 30 FPS -> RTP -> MediaMTX -> WebRTC browser

IMX415 -> /dev/video22 ISP mainpath, 3840x2160 NV12
      -> software JPEG at 5 FPS
      -> /tmp/rk3588_camera_ocr_*.jpg
      -> DocAligner and persistent RKNN PP-OCR
```

The two paths have different owners. The WebRTC stream never queues 4K OCR
frames, and the OCR process never restarts or changes video encoding settings.

## Preview Geometry

The preview capture uses `v4l2-ctl` with an explicit full-sensor selection
before streaming. On this driver, GStreamer's `v4l2src` resets the crop and
produces a roughly 2x zoom. The selected `/dev/video23` path keeps the complete
sensor field while the ISP scales it to 1920x1080, so the preview and OCR paper
geometry match.

The default encoder is 1080p30 H.264 VBR, 12 Mbps target, 20 Mbps peak, and a
worst quantizer of QP 26. Browser-side 4K decoding is intentionally avoided
because it caused visible frame-pacing stalls.

## Full-Text Mode

The 8893 monitor is started with `--text-only`:

```text
paper stable for 0.5 seconds
-> collect two 4K frames
-> select the sharpest valid frame
-> one full-page OCR
-> up to three regional refinements from the second-best frame
-> display full text and evidence boxes
```

The page exposes `collecting_frames`, `ocr_primary`, `ocr_refining`, and
`completed` stages. Review blocks use a warning color and preserve OCR
alternatives; the displayed primary text is never silently corrected.

Identifier rules, patient query controls, patient JSON, and HID auto-entry are
hidden and inactive in this mode. The process does not instantiate their
workers. The compatibility code remains in `camera_ocr_overlay.py` for a later
mode switch, but it is outside the current deployment.

## APIs

```text
GET /api/status  paper, stage, counts, quality, service status
GET /api/result  current matching schema-v2 OCR document
GET /api/config  display and OCR rotation compatibility settings
GET /            live monitor page
```

`/api/result` returns text only when the result `capture_id` matches the live
paper. It includes every OCR block's `source_index`, original `box` and
`polygon`, normalized geometry, confidence, recognition source, and
alternatives. Removing the paper immediately makes the endpoint return no
current document.

The last restricted result remains at:

```text
/run/rk3588-report-parser/verified-full-text.json
```

The writer uses an atomic replacement and mode `0600`. `/run` is cleared by
reboot. No source report image is retained.

## Start and Inspect

```bash
sudo systemctl enable --now rk3588-camera-mediamtx.service \
  rk3588-camera-stream.service \
  rk3588-camera-ocr-snapshots.service \
  rk3588-camera-ocr-overlay.service

systemctl status rk3588-camera-mediamtx.service \
  rk3588-camera-stream.service \
  rk3588-camera-ocr-snapshots.service \
  rk3588-camera-ocr-overlay.service --no-pager
```

Open:

```text
http://<board-ip>:8891/camera
http://<board-ip>:8893/
```

RTSP remains available at:

```text
rtsp://<board-ip>:8555/camera
```

## Stop

```bash
sudo systemctl stop rk3588-camera-stream.service \
  rk3588-camera-ocr-snapshots.service \
  rk3588-camera-mediamtx.service
```

Stopping preview services does not touch the HDMI KVM MediaMTX instance.

## Current Board Check

One physical report completed with:

```text
historical three-frame capture  553 ms
primary OCR          2698 ms
regional refinement 2014 ms
total                5174 ms
result               57 blocks, review_required
```

The monitor rendered the full camera field, paper quadrilateral, OCR boxes,
and full text without browser console errors. The WebRTC publisher remained
ready and available memory was about 2.7 GiB.
