# Camera Full-Text OCR Trigger

## Current Scope

The deployed RK3588 camera service extracts all printed text from one report.
It does not select an identifier, query patient data, call `/scan`, operate HID,
read barcodes, or save the source image.

```text
/dev/video22 3840x2160 JPEG snapshots
-> DocAligner paper detection
-> stable geometry for 0.5 seconds
-> collect exactly two 4K frames
-> select the best frame
-> rectify perspective, rotate 90 degrees, long side 3200
-> one full-page PP-OCR pass
-> optional low-confidence regional refinement from the second-best frame
-> schema-v2 full-text JSON
-> lock until paper removal
```

`rk3588-camera-ocr-snapshots.service` exclusively owns `/dev/video22`. The
trigger only reads `/tmp/rk3588_camera_ocr_*.jpg`; it never opens a V4L2 node.
The independent 1080p30 WebRTC service owns `/dev/video23` and is not restarted
or reconfigured by OCR.

## Trigger and Capture

DocAligner must observe one geometrically stable paper for at least 0.5 seconds
and three observations. Stability uses paper IoU, center movement, and area
change. The first stable event creates a random `capture_id` and starts one
two-frame burst.

Each burst frame must still match the same paper. Valid frames are scored by:

- sharpness;
- edge strength and high-frequency detail;
- glare ratio;
- motion-blur risk;
- contrast.

The highest composite score is the primary OCR frame. The second-highest frame
is used only for regional refinement or an empty-primary full-page retry.

## OCR Budget

The primary image receives exactly one full-page PP-OCR request. A regional
refinement round is considered when:

- a block score is below `0.70`;
- a block is unusually small or touches the rectified image edge;
- the page mean is below `0.65`;
- the primary OCR result is empty.

Overlapping candidates are merged into at most three regions. Each region gets
about two text heights of padding and is enlarged with a 1600-pixel maximum
long side. Refinement runs on the second-best frame.

If primary OCR is empty, only one full-page retry on the second frame is
allowed. The service never falls back to a 12-tile scan. The total OCR budget
is 10 seconds; useful evidence already obtained is returned as
`review_required` when possible.

Refinement results are compared only with one geometrically matching primary
block. A refined block spanning multiple primary blocks is not treated as an
alternative to one of them. Equal text merges evidence. Different text uses
the higher score when the score difference is at least `0.08`; a closer result
keeps the primary text plus one deduplicated alternative and marks the page for
review. No context-based `O/0` or `I/1` correction is applied.

## Concurrency

OCR runs in one background thread. At most one active and one latest pending
job exist. A newer pending capture replaces an older pending capture. The main
loop continues checking paper removal while OCR runs. A completed result is
discarded when its `capture_id` no longer matches the live paper.

## Result Contract

The atomically replaced result file is:

```text
/run/rk3588-report-parser/verified-full-text.json
```

The optional report-center forwarder watches this schema v2 result and sends it
only to the loopback report-center callback. It is independent of `/scan`, the
patient query connector, HID, MSC, Printer and upload processing:

```bash
sudo systemctl enable --now rk3588-camera-report-center-forwarder.service
```

Its non-PHI delivery state is stored at:

```text
/run/rk3588-report-parser/report-center-forwarder.json
```

Its mode is `0600`. It is naturally cleared on reboot because it lives under
`/run`. The top-level status is `accepted`, `review_required`, `rejected`, or
`error`.

```json
{
  "status": "accepted",
  "capture_id": "",
  "created_at": 0,
  "source": {
    "frame_size": {},
    "paper_corners": [],
    "ocr_rotation": 90,
    "ocr_document_long_side": 3200,
    "selected_frame_sha256": ""
  },
  "quality": {},
  "timings": {},
  "reasons": [],
  "document": {
    "schema_version": 2,
    "image_size": [],
    "full_text": "",
    "lines": [],
    "blocks": []
  }
}
```

Each block preserves `id`, `source_index`, `line_id`, `text`, `score`, `box`,
`polygon`, `normalized_box`, `normalized_polygon`, `recognition_source`, and
`alternatives`. Line grouping uses vertical overlap, center distance, local
text height, then horizontal order. Raw blocks remain available as evidence.

The public trigger status excludes OCR text. It exposes only state, counts,
quality, timings, and the current capture ID. The 8893 result endpoint returns
text only while its capture ID matches the live paper. Removing the paper hides
the page result immediately but does not erase the last restricted `/run` file.

## Status Stages

```text
tracking           paper present but not stable
collecting_frames  collecting the two quality frames
queued             waiting behind one active OCR job
ocr_primary        full-page OCR running
ocr_refining       regional or empty-primary retry running
completed          accepted, review_required, rejected, or error result ready
locked             waiting for paper removal
```

## Service

```bash
python3 scripts/camera_paper_trigger.py \
  --text-only \
  --frame-glob '/tmp/rk3588_camera_ocr_*.jpg' \
  --paper-model runtime/docaligner/lcnet050_p_multi_decoder_l3_d64_256_fp32.onnx \
  --detector-backend onnxruntime \
  --ocr-endpoint http://127.0.0.1:5002/ocr \
  --ocr-rotation 90 \
  --ocr-document-long-side 3200 \
  --burst-frames 2 \
  --ocr-total-budget 10 \
  --status-file /run/rk3588-report-parser/camera-trigger.json \
  --result-file /run/rk3588-report-parser/verified-identifier.json \
  --full-text-result-file /run/rk3588-report-parser/verified-full-text.json
```

The compatibility identifier/A-B path still exists when `--text-only` is
omitted, but it is not part of the current board deployment.

## Verified Runtime

- Board: ATK-DLRK3588, Linux 5.10.160, Python 3.9.2.
- DocAligner ONNX Runtime P95: 17.740 ms over 100 iterations.
- Persistent PP-OCR RKNN worker kernel time: about 245-331 ms per request.
- One real report: 553 ms burst, 2698 ms primary OCR, 2014 ms refinement,
  5174 ms total, 57 blocks.
- Result permission: `0600`; patient lookup and HID status: disabled.

Exact worker, model, dictionary, `librknnrt.so`, and DocAligner hashes are in
`runtime/manifest.json`.

## Known Limitation

DocAligner expects a complete or nearly complete document. A report with a
large edge outside the camera view may not trigger. Partial-strip presentation
needs a separate fallback detector and is outside this full-page first stage.
