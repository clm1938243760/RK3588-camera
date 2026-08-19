# RK3588 Camera Capture

RK3588 CSI camera capture and report monitoring module for the ATK-DLRK3588
board. This repository contains the camera preview, paper-capture monitor,
OCR result display, and the patient-query integration used by the current
board deployment.

## v0.1

The v0.1 pipeline is:

```text
IMX415 CSI camera
  -> /dev/video23 1920x1080 NV12
  -> MPP H.264 30 FPS
  -> MediaMTX WebRTC preview :8891
  -> latest-only JPEG snapshots at 5 FPS
  -> DocAligner paper trigger and local PP-OCR
  -> 8893 monitor page
```

After two identical identifier captures, the monitor can independently:

```text
patient query -> POST 127.0.0.1:8080/patient/query -> patient JSON
auto entry    -> POST 127.0.0.1:8080/scan          -> existing HID workflow
```

The two actions have separate switches. Patient lookup does not create a scan
event and does not operate HID. The current release keeps patient JSON lookup
enabled by default and automatic HID entry disabled by default.

## Repository Contents

- `camera_stream_mpp.sh`: camera capture, WebRTC encoder, and OCR snapshot branch.
- `camera_stream_watchdog.sh`: restarts a stalled camera encoder only.
- `camera_mediamtx.yml`: separate WebRTC/RTSP configuration for the CSI camera.
- `camera_ocr_overlay.py`: 8893 monitor, OCR display, patient JSON state, and settings.
- `start_camera_preview.sh` / `stop_camera_preview.sh`: manual preview controls.
- `systemd/`: boot services for MediaMTX, the stream, and the monitor.
- `docs/CAMERA_PREVIEW.md`: board-side data path and operation guide.
- `tests/`: configuration, API, result isolation, and action-state tests.

The camera trigger process and RKNN PP-OCR service remain maintained in the
`rk3588_report_parser` and `rk3588_gateway_rk3588` repositories respectively.

## Board Install

The current board deployment path is `/opt/rk3588_kvm`:

```bash
sudo systemctl enable --now rk3588-camera-mediamtx.service
sudo systemctl enable --now rk3588-camera-stream.service
sudo systemctl enable --now rk3588-camera-ocr-overlay.service
```

Open:

```text
http://<board-ip>:8891/camera   # camera preview
http://<board-ip>:8893/          # report monitor
http://<board-ip>:8893/api/patient
```

The patient result file is:

```text
/run/rk3588-report-parser/verified-patient.json
```

The JSON keeps the upstream envelope and record field names:
`code`, `data`, `msg`, `success`, with all returned records preserved.

## Local Verification

```powershell
python -m py_compile camera_ocr_overlay.py tests/test_camera_ocr_overlay.py
python -m unittest discover -s tests -v
```

Release tag: `v0.1`.
