# CSI Camera Preview

This is a standalone low-latency preview for the CSI camera. It does not use
the HDMI RX device or the MediaMTX process owned by the KVM service.

## Data Path

```text
IMX415 -> /dev/video23 ISP selfpath -> 1920x1080 NV12 -> MPP H.264 -> RTP
      -> local Camera MediaMTX -> WebRTC browser or RTSP client
```

`/dev/video23` is the RKISP hardware-scaled selfpath for the IMX415 camera.
The 1080p30 stream is used because browser-side 4K decoding caused visible
frame pacing stalls even though the board encoder remained healthy.

The default quality profile uses 12 Mbps VBR with a 20 Mbps peak and limits the
worst quantizer to QP 26. This preserves document text and fine edges better
than the earlier 6 Mbps/QP 32 profile while keeping a stable 30 FPS.

## Start

The persistent services start automatically after installation:

```bash
sudo systemctl enable --now rk3588-camera-mediamtx.service rk3588-camera-stream.service
```

Service status:

```bash
systemctl status rk3588-camera-mediamtx.service rk3588-camera-stream.service --no-pager
```

For a temporary manual session instead, use:

```bash
sudo /opt/rk3588_kvm/start_camera_preview.sh
```

Open the WebRTC player in a browser on the same network:

```text
http://<board-ip>:8891/camera
```

Open the live report-capture monitor at:

```text
http://<board-ip>:8893/
```

The stream pipeline writes latest-only JPEG snapshots for the independent
DocAligner trigger. The monitor reads the trigger status file and draws the
paper quadrilateral, stability progress, A/B capture progress, and final
verification state over the 30 FPS WebRTC video. It does not run another OCR
worker and never includes the extracted identifier value in its public status.

After the A/B identifier match, the monitor has two independent actions:

```text
Patient query  -> POST http://127.0.0.1:8080/patient/query -> patient JSON
Auto entry     -> POST http://127.0.0.1:8080/scan          -> HID workflow
```

Both actions have separate switches, deduplication state, and retries. The
patient query route is loopback-only and does not queue a scan event or start
HID. The patient response keeps the upstream `code`, `data`, `msg`, and
`success` envelope and every returned record.

The current patient response is available while the matching report remains
active:

```bash
curl -s http://127.0.0.1:8893/api/patient
```

The last response is atomically retained for local consumers with mode `0600`:

```text
/run/rk3588-report-parser/verified-patient.json
/run/rk3588-report-parser/verified-patient.meta.json
```

Only the metadata file contains `capture_id`; the patient JSON itself remains
schema-compatible with the hospital patient API. A metadata mismatch makes
the web endpoint return `PENDING`, preventing a stale patient from being paired
with a new report.

An RTSP client can use:

```text
rtsp://<board-ip>:8555/camera
```

## Stop

```bash
sudo systemctl stop rk3588-camera-stream.service rk3588-camera-mediamtx.service
```

The camera preview has its own local MediaMTX ports and PID files, so starting
or stopping it does not restart the HDMI KVM stream.
