# RK3588 Camera Runtime Snapshot - 2026-08-18

This document records the observed state of an isolated ATK-DLRK3588
development board. It is the baseline for subsequent camera work. Repository
defaults and older conversation notes must not override this runtime state.

No board service was stopped, restarted, or reconfigured during this audit.
Patient identifier text was not printed or copied.

## Board

```text
Host:          ATK-DLRK3588
OS:            Debian 11, arm64
Kernel:        5.10.160
Memory:        3.8 GiB total, about 2.9 GiB available
Swap:          disabled
Root storage:  14 GiB total, 12 GiB used, 1.4 GiB free (90%)
```

The low free disk space is the most immediate board-level maintenance risk.

## Effective Pipeline

```text
/dev/video23, 1920x1080 NV12/NM12 at 30 FPS
  -> one GStreamer owner and tee
     -> leaky queue (one frame)
        -> Rockchip MPP H.264 High@L4
        -> 12 Mbps target, 6-20 Mbps bounds, GOP 30
        -> RTP/UDP localhost:5006
        -> dedicated MediaMTX instance
        -> WebRTC camera preview
     -> leaky queue (one frame)
        -> videorate drop-only to 5 FPS
        -> CPU jpegenc quality 90
        -> /tmp/rk3588_camera_ocr_*.jpg, maximum three files
        -> DocAligner paper trigger
        -> local RKNN PP-OCR
        -> configured character-count identifier extraction
        -> two-pass exact-match verification
```

`/dev/video23` has exactly one owner: the CSI GStreamer process. Recognition
only reads the generated JPEG files. A second reader must not be added.

`/dev/video40` is independently owned by the HDMI KVM pipeline. The CSI camera
and HDMI capture therefore do not contend for the same V4L2 device.

Do not use `mppjpegenc` for the OCR branch on this image. The verified branch
uses `videoconvert` plus CPU `jpegenc`.

## Trigger State Machine

The deployed recognition sequence is:

1. DocAligner detects a complete or nearly complete document and four corners.
2. Geometry must remain stable for at least 0.8 seconds.
3. One OCR request confirms that text exists.
4. Three new frames are collected and scored for sharpness, detail, glare, and
   motion blur. The best frame becomes field A.
5. After at least 0.2 seconds, three more frames are collected independently.
   Their best frame becomes field B.
6. A and B are accepted only when both are unique and byte-for-byte identical.
7. The result remains locked until the paper has been absent continuously for
   0.5 seconds.

The service does not correct `O/0`, `I/1`, case, or other OCR characters. It
does not save patient images. The accepted identifier is the only sensitive
value written to disk, at:

```text
/run/rk3588-report-parser/verified-identifier.json
```

The file mode is `0600`. Its observed structure was:

```text
status:       string
identifier:   string, length 11 (value not inspected or disclosed)
verification:string
attempt:      integer
created_at:   number
```

## Effective Configuration

The current parser path is OCR-only. Qwen/RKLLM is not called.

```text
Parser profile:        rk3588-ocr-count-only
Active rule profile:   single-length-11
Character set:         digits
Configured lengths:    [11]
Unlabelled values:     allowed
Forward to gateway:    false
```

The mutable board rule is:

```text
/var/lib/rk3588-report-parser/active_identifier_rules.json
```

It intentionally differs from the repository example
`runtime/active_identifier_rules.json`, which still contains the older
16-character alphanumeric example. Future work must read the board's mutable
rule when determining live behavior.

Rotation has two layers. The web setting stores rotations relative to the
current orientation, while the service uses a 90-degree base:

```text
Stored display rotation: 0 degrees relative
Stored OCR rotation:     90 degrees relative
Generated environment:   OCR_ROTATION=180
Running process:         --ocr-rotation 180
```

The running process argument is the final authority.

## Services And Ports

All of the following camera services were `active`, `enabled`, and had zero
restarts since their current successful start:

| Service | Purpose |
| --- | --- |
| `rk3588-camera-mediamtx.service` | Dedicated CSI MediaMTX/WebRTC server |
| `rk3588-camera-stream.service` | Sole CSI capture, H.264 and JPEG tee |
| `rk3588-camera-ocr-overlay.service` | Port 8893 status/configuration page |
| `rk3588-report-camera-trigger.service` | DocAligner, burst selection, OCR, A/B verification |

Related services also observed active and enabled were
`rk3588-report-parser.service`, `rk3588-ppocr.service`,
`rk3588-gateway.service`, and `rk3588-kvm.service`.

| Port | Runtime role |
| --- | --- |
| `5002` | Local RKNN PP-OCR service |
| `8020` | OCR-only identifier parser web/API |
| `8080` | Gateway |
| `8090` | HDMI KVM |
| `8893` | Camera recognition monitor/configuration |
| `8889`, `8892`, `8189` | HDMI KVM MediaMTX/WebRTC |
| `8891`, `8191`, `8555` | CSI camera MediaMTX/WebRTC/RTSP |
| `8554` | HDMI KVM RTSP |

Health checks succeeded for ports 5002, 8020, and 8893.

## Observed Runtime

At the end of the audit the paper was still present:

```text
state:                locked
reason:               waiting_for_paper_removal
capture_stage:        verified
paper_confidence:     about 0.998-0.999
paper inference:      about 15 ms
text blocks:          26
initial OCR:          about 2.0 seconds
verification:         accepted, exact_match, attempt 1
```

The trigger's `processed_frames` counter resets whenever that process starts;
it is not a lifetime board counter.

Approximate live resource use:

```text
CSI GStreamer:       33.4% CPU, 35 MB RSS
DocAligner trigger:  26.1% CPU, 174 MB RSS
Camera MediaMTX:     16.6% CPU, 59 MB RSS
Monitor web service:  3.5% CPU, 20 MB RSS
```

The locked DocAligner benchmark in `runtime/manifest.json` remains:

```text
Model load:          603 ms
Mean inference:      17.683 ms
P50:                 17.683 ms
P95:                 17.740 ms
Peak RSS:            151.16 MB
```

The deployed runtime is Python 3.9.2, OpenCV 4.5.1, NumPy 1.23.5, and ONNX
Runtime 1.17.3 with one thread and sequential execution.

## Integrity Check

The following deployed files matched their local counterparts byte-for-byte by
SHA-256:

| File | SHA-256 |
| --- | --- |
| `scripts/camera_paper_trigger.py` | `7b247f8eab3d3033a141b0937b8189f438a107dce881ce2506e9e664bb010ef0` |
| `paper_detector.py` | `9df82512eb94392616ad5e0677e54de241ca1c5b5da6bff5930093b548ce540a` |
| `paper_trigger.py` | `198edb0d91ae842308ccdd24e6eb0fffed09186454bc2e5cd64bb91767696d2b` |
| `frame_source.py` | `eca7e21c7a25decd9ac0d0ed447b9320cfd8670f736aabf45b4369afa8e2e6e6` |
| `capture_trigger.py` | `08c80ad711cb29459162a805d5bdc39f28e52f26d7340fc9a2e1f550368c8aa3` |
| `frame_quality.py` | `287322e971580b94be59202c23ca31cb33c9d0d003978d02a2d926dbad545c15` |
| `capture_identifier.py` | `bc811dabce26e1911ec11baaa13bf36a5be44d4544c7927f3653499ae1f4277b` |
| `runtime/manifest.json` | `2a9e9a96800228b94d15d5e76e10214a6e804be4d745a1ae62df81b1f94d49a0` |
| `rk3588-report-camera-trigger.service` | `0e6a7aac65a9ee3066ba265ac857c396a184c43a47523f5778012006c923a19b` |
| `camera_ocr_overlay.py` | `fccaebe4e6cd27cd935ab07a903433a888507140bd7ec0a3faf76bdb1919abf7` |
| `camera_stream_mpp.sh` | `a8a88aed19e8cb9deaa48a11ae828a9ad91786b201dd7174566bab09adf1467f` |

The DocAligner model SHA-256 is:

```text
32d186080ce16442674d4c0eaaaaac878eea289b56a8d1284f05fff1ff42e220
```

The local camera-focused regression suite was rerun against this source
snapshot. All 35 tests passed, covering paper detection and stability, JPEG
frame intake, OCR trigger behavior, three-frame quality selection, A/B exact
matching and retries, privacy-safe status output, and manifest locking.

## Known Issues

1. MediaMTX logged many `reader is too slow` warnings. There were two WebRTC
   readers during the audit, and 22 warnings in the final ten-minute window.
   The source path remained ready with zero inbound frame errors. This points
   to slow or inactive browser readers, not a CSI capture failure.
2. `rkaiq_3A_server` is running, while unused `rkcif` virtual channels produce
   repeated `remote terminal sensor failed` / `-19` messages. The active
   `/dev/video23` stream remained healthy. No OOM, MPP reset, or encoder hang
   was found in the current boot log.
3. Historical journal entries contain failed start attempts caused by a busy
   camera device and an occupied port. All four current camera services have
   `Result=success` and `NRestarts=0`; those entries are not current failures.
4. The existing `CAMERA_PAPER_TRIGGER.md` deployment paragraph says the camera
   services are disabled and inactive. That statement is stale; the live board
   has them enabled and active.
5. DocAligner expects a complete or nearly complete paper. Presenting only a
   narrow identifier strip remains the primary functional gap.

## Rules For Subsequent Work

- Treat this snapshot and a fresh board check as the runtime source of truth.
- Preserve the single ownership of `/dev/video23`.
- Add camera consumers through the existing tee/JPEG output, not a second V4L2
  capture process.
- Keep OCR and identifier text out of public status, logs, and normal debug
  files.
- Do not assume the repository's 16-character example is active.
- Address partial-paper detection as a fallback around the current DocAligner
  gate; do not discard the verified full-document path.
- Measure WebRTC lag per reader before changing the healthy encoder pipeline.
- Free disk space before installing additional models or retaining captures.
