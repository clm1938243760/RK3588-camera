# Medical Application Identifier Parser

`rk3588-report-parser` extracts one configured-length identifier from a printed
medical application image. It is offline, template-free, and does not inspect
QR codes or barcodes.

```text
JPEG/PNG -> image quality diagnostics -> local PP-OCR -> alphanumeric tokens
-> configured character count -> uniqueness check -> JSON
```

The web UI configures one target character count. Rule mode does not call a
language model and does not infer whether the value is a patient ID, request
number, report number, or another business type:

```text
one distinct match  -> accepted
multiple matches    -> review_required, no value selected
no match            -> rejected
```

In single-length mode, resolution, blur, contrast, OCR item count, and OCR
confidence are diagnostic warnings only. They never decide the final status.
Every ASCII alphanumeric run is evaluated by character count, including values
that resemble phone numbers or other legacy excluded types.

The selected value is copied exactly from OCR evidence. OCR substitutions such
as `O/0` and `I/1` are never corrected. See `identifier_rules.example.json`.

The current camera deployment uses a separate `--text-only` path. It disables
identifier selection, patient lookup, and HID entry, then produces one complete
schema-v2 OCR document from a stable two-frame 4K capture. The legacy
identifier CLI and web service remain available for desktop compatibility.

EXIF orientation is applied during decode and PaddleOCR uses its local angle
classifier. When OpenCV is available, a document quadrilateral is rectified
only after strict area, convexity, rectangularity, and confidence checks; OCR
coordinates are transformed back to the original image for evidence display.

## Result

```text
identifier: "P2540558"
```

The compatibility response still includes `primary_identifier` and evidence
arrays, but `identifier` is the single business value applications should read.

## Desktop smoke test

For OCR-only rules, start only the loopback OCR service. Start the model service
only when rules are disabled and the compatibility model path is needed:

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
& "$root\scripts\start_pc_ocr.ps1" -Python "$root\.venv-ocr\Scripts\python.exe" -BootstrapModels
& "$root\scripts\start_pc_model.ps1" -Python "$root\.venv-pc\Scripts\python.exe" -ModelPath "$root\pc_models\Qwen2.5-1.5B-Instruct" -Device cuda:0
```

Parse one image:

```powershell
$env:PYTHONPATH = "$root\src"
& "$root\.venv-pc\Scripts\python.exe" -m rk3588_report_parser `
  --config "$root\config.pc.example.json" `
  --image "D:\report_samples\sample.jpg" `
  --output "D:\report_samples\result.json" `
  --allow-unverified-runtime
```

Exit codes are `0` for accepted, `1` for review/rejected, and `2` for an
operational error. `--debug-dir` explicitly writes sensitive OCR diagnostics.
The original image is never copied.

The original nine-field prototype remains available with `--all-fields` and
its legacy linker/association options.

## Local web service

```powershell
$env:PYTHONPATH = "$root\src"
& "$root\.venv-pc\Scripts\python.exe" -m rk3588_report_parser.web_server `
  --config "$root\config.pc.example.json" `
  --allow-unverified-runtime
```

Open `http://127.0.0.1:8020`. The UI displays the uploaded image, evidence boxes,
primary identifier, classified identifiers, alternatives, and timings. Number
rules can be edited from the toolbar and are persisted to
`runtime/active_identifier_rules.json`. The server processes report images in
memory and does not retain them.

API endpoints:

```text
POST /api/v1/parse       multipart field: image
GET  /api/v1/health
GET  /api/v1/runtime
GET  /api/v1/rules
PUT  /api/v1/rules       JSON rule profile
GET  /
```

A non-loopback listener requires `RK3588_REPORT_ACCESS_TOKEN` and Bearer
authentication.

## Runtime profiles

```text
config.edge.example.json  RK3588 RKLLM W8A8 baseline
config.pc8.example.json   8 GB NVIDIA profile
config.pc12.example.json  12 GB NVIDIA profile
config.pc.example.json    current desktop development model
config.rk3588.ocr_only.json  RK3588 PP-OCRv4 RKNN, character-count only
```

The profile model names describe the compatibility model path. OCR-only rules
do not load or call that model. Any model-based deployment still requires exact
runtime, model, OCR files, and SHA-256 values in `runtime/manifest.json`.

## RK3588 OCR-only service

The edge service reuses the existing `rk3588-ppocr.service` endpoint on port
5002. It does not start or call Qwen. The supplied systemd unit listens on port
8020 and keeps editable rule state in
`/var/lib/rk3588-report-parser/active_identifier_rules.json`.

```text
/opt/rk3588_report_parser
/etc/rk3588-report-parser.env
/etc/systemd/system/rk3588-report-parser.service
/var/lib/rk3588-report-parser
```

Non-loopback access requires `RK3588_REPORT_ACCESS_TOKEN` in the environment
file. The checked-in edge configuration starts with one 16-character target
rule; it can be changed from the web UI without restarting the service.

## Camera paper trigger

The optional camera trigger reads rotating 3840x2160 JPEG snapshots from the
independent `/dev/video22` OCR service. It never opens a V4L2 device itself.
DocAligner runs on each low-rate snapshot. After the same paper remains stable
for 0.5 seconds, exactly two 4K frames are scored for sharpness, detail,
glare, and motion blur. The best frame receives one full-page PP-OCR pass. Up
to three low-confidence regions may be re-read from the second-best frame. On
RK3588, documents whose aspect ratio exceeds 1.25 are split into up to four
overlapping near-square tiles before the fixed 480x480 RKNN detector. Tile
coordinates are mapped back to the rectified page before line assembly.

```text
DocAligner -> stable 0.5 s -> two 4K frames -> best frame
-> adaptive overlapping OCR tiles -> coordinate merge
-> optional regional refinement -> schema v2 JSON
```

The OCR worker has one active job and one latest pending slot. A result is
discarded if its `capture_id` is no longer current when OCR completes. The
The web monitor labels whether the result belongs to the current capture and
keeps the last valid result available after the paper is removed.

```bash
python3 scripts/camera_paper_trigger.py \
  --text-only \
  --frame-glob '/tmp/rk3588_camera_ocr_*.jpg' \
  --paper-model runtime/docaligner/lcnet050_p_multi_decoder_l3_d64_256_fp32.onnx \
  --ocr-endpoint http://127.0.0.1:5002/ocr \
  --burst-frames 2 \
  --ocr-document-long-side 3200 \
  --ocr-tile-max-aspect 1.25 \
  --ocr-tile-overlap-ratio 0.15 \
  --ocr-tile-max-count 4 \
  --ocr-total-budget 10 \
  --status-file /run/rk3588-report-parser/camera-trigger.json \
  --result-file /run/rk3588-report-parser/verified-identifier.json \
  --full-text-result-file /run/rk3588-report-parser/verified-full-text.json
```

The detector backend is `auto`: OpenCV DNN is tried first, followed by ONNX
Runtime for older OpenCV builds. Status output contains counts and state only;
it does not store report images or OCR text. The RK3588 ONNX Runtime path has
been benchmarked at 17.740 ms P95 and 151.16 MB peak RSS over 100 single-thread
iterations. The deployed persistent RKNN worker completed one real report in
5.174 seconds including frame collection and three regional OCR calls.
The camera trigger service is installed disabled until explicitly enabled.

See [Camera Paper Trigger](docs/CAMERA_PAPER_TRIGGER.md) for the state contract,
runtime files, and current limitations.

## Development

```powershell
$env:PYTHONPATH = "$root\src"
& "$root\.venv-pc\Scripts\python.exe" -m compileall "$root\src"
& "$root\.venv-pc\Scripts\python.exe" -m unittest discover -s "$root\tests" -v
```

Run the identifier release benchmark on deidentified OCR fixtures:

```powershell
& "$root\.venv-pc\Scripts\python.exe" -m rk3588_report_parser.identifier_evaluation `
  --dataset "D:\report_dataset\blind.jsonl" `
  --config "$root\config.pc12.example.json" `
  --output "D:\report_dataset\benchmark.json" `
  --enforce-deployment-targets
```

The gate requires at least 100 blind samples, zero false accepts, accepted
identifier precision and primary accuracy of 99.5%, and core recall of 95%.

See [Identifier System Design](docs/IDENTIFIER_SYSTEM.md) for the wire contract,
adjudication rules, release gates, and RK3588 rollout order.

## UIE desktop experiment

An isolated desktop branch can map existing PP-OCR blocks to the same
14-field patient-query JSON with `uie-base`, `uie-medical-base`, or UIE-X.
Every returned value must match an exact OCR substring and retains source span
IDs and boxes. No UIE model is enabled on the RK3588 services.

See [UIE Patient-Field Experiment](docs/UIE_EXPERIMENT.md) for installation,
commands, compatibility pins, and the current four-sample smoke comparison.

The PC-first patient console runs independently from the legacy identifier UI:

```powershell
& "$root\scripts\start_uie_web.ps1"
```

Open `http://127.0.0.1:8030/`. Uploaded images follow
`image -> PP-OCR -> uie-base -> exact OCR evidence -> patient-query JSON`.
Camera schema v2 OCR results can be submitted directly to
`POST /internal/v1/uie/extract`; this path reuses the camera OCR blocks and
does not perform OCR again.
