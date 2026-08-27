# UIE patient-field experiment

This branch evaluates PaddleNLP UIE after the existing PP-OCR stage. It is a
desktop experiment only. It does not change the RK3588 camera, OCR, gateway,
HID, scanner, MSC, or Printer services.

## Evidence contract

```text
PP-OCR blocks and coordinates
-> UIE field spans
-> exact start/end and source-text check
-> OCR span IDs and boxes
-> deterministic patient-query JSON
```

UIE is not allowed to create patient text. A prediction is discarded unless
its text exactly equals the OCR source substring at the returned offsets. The
raw field evidence keeps the original OCR spelling. Only the patient-query
view performs deterministic formatting such as `63岁` to `63` and
`1988年04月14日` to `1988-04-14`.

Every refined or fallback value is also an exact continuous OCR substring.
No fallback corrects OCR characters or supplies text that was absent from the
OCR evidence.

## Install

Use the isolated Python 3.10 environment. Do not install these packages into
`.venv-ocr`.

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
& "$root\scripts\setup_uie_env.ps1"
```

PaddleNLP 2.8.1 declares an unavailable `tool-helpers` dependency in its wheel.
The setup script installs the pinned runtime dependencies first and then
installs PaddleNLP with `--no-deps`. `aistudio-sdk` is fixed at 0.2.6 because
newer versions removed the `download` function required by PaddleNLP 2.8.1.

The first run downloads model weights to the current user's PaddleNLP cache.
Approximate weight sizes are 450 MB for each text model and 1.05 GB for UIE-X.

## Run

### Patient extraction web

Start the existing desktop PP-OCR service first, then run the UIE console on
port 8030:

```powershell
$root = "D:\documents\New project\rk3588_report_parser"

& "$root\scripts\start_pc_ocr.ps1" `
  -Python "$root\.venv-ocr\Scripts\python.exe" `
  -BootstrapModels

& "$root\scripts\start_uie_web.ps1"
```

Open `http://127.0.0.1:8030/`. The console supports:

- JPEG/PNG upload through `POST /api/v1/parse`.
- Current camera OCR schema v2 through `POST /internal/v1/uie/extract`.
- Chinese patient fields, OCR evidence boxes, full OCR text, and patient JSON.
- Primary prompts, prompt aliases, required-field, and minimum-confidence configuration.
- Save-and-rerun from the configuration dialog for the currently selected image.
- OCR-backed alternative selection when several prompts resolve the same field
  to different values.
- Field-type validation prevents cross-type UIE predictions, such as an age
  being written to `sex` or a numeric row prefix being written to `exam_item`.
- Identifier predictions may be tightened to one exact alphanumeric OCR token
  when UIE includes adjacent line text; the original UIE range remains in
  `raw_value` for audit.
- Missing values can be recovered from an exact configured label and its inline
  or neighboring OCR block. Sex, age, and birthday also support a conservative
  unique typed-value fallback when the label itself was recognized poorly.
- The web console shows each field's resolution method, rejected model reasons,
  and an OCR-backed confirmation action for fields that still require review.
- Latest same-structure patient response through `GET /api/v1/patient`.

The camera endpoint consumes the existing OCR blocks and never runs a second
OCR pass. Duplicate `capture_id` values are idempotent. The latest full result
is available from `GET /api/v1/result` and is atomically written to
`runtime/latest_uie_patient.json` with restrictive permissions. Input images
are not retained.

The active web configuration is stored in
`runtime/active_uie_schema.json`. Changing it updates PaddleNLP Taskflow's
schema without restarting the service.

Each field supports one primary prompt and up to eight aliases. The complete
schema is limited to 32 prompts to bound latency. Different values returned
for one field force `review_required`; the web console exposes those values as
selectable OCR-backed alternatives. Selecting one rebuilds the patient JSON,
records only its source span IDs as the manual-correction audit entry, and
never permits free-text patient values.

Text-only UIE:

```powershell
& "$root\.venv-uie\Scripts\rk3588-report-uie.exe" `
  --ocr-json "D:\report_samples\debug\identifier_sample1_final\ocr_spans.json" `
  --schema "$root\config.uie.example.json" `
  --model uie-base `
  --output "$root\evaluation_output\uie-base-sample1.json"
```

Use `--model uie-medical-base` for the medical-domain checkpoint.

UIE-X with image and existing OCR boxes:

```powershell
& "$root\.venv-uie\Scripts\rk3588-report-uie-x.exe" `
  --image "D:\report_samples\sample.jpg" `
  --ocr-json "D:\report_samples\debug\sample\ocr_spans.json" `
  --schema "$root\config.uie.example.json" `
  --output "$root\evaluation_output\uie-x-sample.json"
```

UIE-X receives a temporary A4-padded copy and translated boxes to work around
PaddleNLP 2.8.1 applying its horizontal padding offset to vertical boxes. The
temporary image is deleted immediately after inference.

For labeled, deidentified OCR fixtures, run the aggregate text-model benchmark:

```powershell
& "$root\.venv-uie\Scripts\rk3588-report-uie-evaluate.exe" `
  --dataset "D:\report_dataset\uie_blind.jsonl" `
  --schema "$root\config.uie.example.json" `
  --model uie-base `
  --output "$root\evaluation_output\uie-base-benchmark.json"
```

The aggregate report contains field precision, recall, evidence trace rate,
and latency. Its sample rows contain mismatch field names but do not repeat
patient values.

## Four-sample smoke result

The current four OCR fixtures are only a smoke test and do not establish
accuracy. All models used the same nine prompts and CPU inference.

| Model | Mean UIE time | Max UIE time | Observed behavior |
|---|---:|---:|---|
| `uie-base` | 4.62 s | 7.64 s | Best text-only coverage; found labeled patient IDs in samples 2 and 3 |
| `uie-medical-base` | 4.62 s | 7.30 s | Lower useful coverage and one obvious field-type error |
| `uie-x-base` | 18.39 s | 29.80 s | Fewer conflicts and useful layout result in sample 4, but much slower |

None of the three zero-shot models extracted the unlabeled long request number
in sample 1. UIE-X did not solve that case despite receiving the image and
layout. The current recommendation is therefore `uie-base` as the desktop
baseline, followed by manually labeled few-shot fine-tuning. UIE-X remains an
optional comparison for layouts where repeated values cannot be separated by
text alone.

Official reference: [PaddleNLP UIE model documentation](https://github.com/PaddlePaddle/PaddleNLP/blob/develop/slm/model_zoo/uie/README.md).
