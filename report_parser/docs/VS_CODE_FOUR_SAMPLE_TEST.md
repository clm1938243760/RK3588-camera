# VS Code Four-Sample Desktop Test

This is a desktop-only test. Do not copy anything to RK3588 during this stage.
Keep report images outside the Git worktree, for example:

```text
D:\report_samples\
  sample01.jpg
  sample02.jpg
  sample03.png
  sample04.jpg
  output\
```

Only use reports approved for testing. The services below bind to `127.0.0.1`
only, but the input image and extracted fields are still personal data on the
local workstation. Do not add raw images, OCR dumps, or debug overlays to Git.

## 1. Prepare the two Python environments

The existing `.venv-pc` contains the Qwen desktop model runtime. PaddleOCR must
run in a separate CPython 3.10 environment because this workstation currently
has Python 3.14 only.

Install CPython 3.10, then confirm it is visible:

```powershell
py -3.10 --version
```

In the VS Code terminal opened at the project root:

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
py -3.10 -m venv "$root\.venv-ocr"
& "$root\.venv-ocr\Scripts\python.exe" -m pip install --upgrade pip
& "$root\.venv-ocr\Scripts\python.exe" -m pip install -r "$root\requirements-pc-ocr.txt"
```

The first PaddleOCR startup below uses `-BootstrapModels`. That explicitly
allows model-file download only; it does not upload report images. Keep this
switch during the four-sample desktop test: after the first download it reuses
the local PaddleOCR cache. Use explicit local model directories later when
preparing a controlled deployment environment.

## 2. Terminal A: start local PP-OCR

Open a VS Code terminal and keep it running:

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
& "$root\scripts\start_pc_ocr.ps1" `
  -Python "$root\.venv-ocr\Scripts\python.exe" `
  -BootstrapModels
```

Wait until it prints a ready message. In another terminal, verify it:

```powershell
Invoke-RestMethod http://127.0.0.1:5002/health
```

Expected result includes `ok: True` and `backend: paddleocr_desktop`.

## 3. Terminal B: start local Qwen

Open a second VS Code terminal and keep it running:

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
& "$root\scripts\start_pc_model.ps1" `
  -Python "$root\.venv-pc\Scripts\python.exe" `
  -ModelPath "$root\pc_models\Qwen2.5-1.5B-Instruct" `
  -Device cuda:0
```

Verify it from a third terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
```

## 4. Terminal C: parse each report image

Run this once per image. The current CLI validates only `patient_id`. The
`evidence_chat` linker asks Qwen for a small JSON decision: first select the OCR
span that semantically acts as the patient-ID label, then select the value span
governed by that label. Normal chat generation gives the 1.5B model enough room
for the semantic decision while the parser still accepts OCR span IDs only.
The `evidence` association mode accepts only that model-selected evidence after
checking span existence, label/value geometry, OCR confidence, and field
format. There is no hospital keyword table, fixed coordinate template, or ROI.
The model never generates patient text; the program reconstructs the ID from
the selected OCR span. Use `--all-fields` only for the legacy nine-field test.
`--allow-unverified-runtime` is required only because the RK3588 runtime
manifest is intentionally not approved yet.

```powershell
$root = "D:\documents\New project\rk3588_report_parser"
$python = "$root\.venv-pc\Scripts\python.exe"
$env:PYTHONPATH = "$root\src"
New-Item -ItemType Directory -Force "D:\report_samples\output" | Out-Null

& $python -m rk3588_report_parser `
  --config "$root\config.pc.example.json" `
  --image "D:\report_samples\<actual-image-filename>.jpg" `
  --output "D:\report_samples\output\sample01.json" `
  --ocr-endpoint "http://127.0.0.1:5002/ocr" `
  --llm-endpoint "http://127.0.0.1:8010/v1/chat/completions" `
  --linker-mode evidence_chat `
  --association-mode evidence `
  --allow-unverified-runtime

$LASTEXITCODE
Get-Content -Raw "D:\report_samples\output\sample01.json"
```

Replace `<actual-image-filename>.jpg` with the exact name shown by:

```powershell
Get-ChildItem -File "D:\report_samples"
```

The command prints non-sensitive stages such as OCR, field association, and
validation while it runs. Evidence selection plus confirmation currently takes
roughly 30 to 60 seconds per page on the desktop 1.5B model; wait
for the final `complete: accepted` or `complete: rejected` message before reading the
output JSON.

Windows PowerShell 5 may display UTF-8 Chinese JSON as mojibake with plain
`Get-Content`. Use this when inspecting a result in the terminal:

```powershell
Get-Content -Raw -Encoding UTF8 "D:\report_samples\output\sample01.json"
```

Exit code meanings:

- `0`: accepted; manually compare each returned field with the source report.
- `1`: rejected by image quality or field validation; save the rejection
  reasons, because rejection is safer than a wrong accepted record.
- `2`: operational failure; first check the two `/health` endpoints above.

Do not use `--debug-dir` for all samples. For one controlled failure analysis
only, it can create an OCR overlay and OCR text dump, which are sensitive data.

## 5. Record the four outcomes

For each sample, make a small manual record:

```text
sample id | result status | patient ID exact | report no exact | name exact |
sex exact | birthday exact | exam item exact | rejection reason / note
```

Do not treat four successful samples as a deployment pass. They are a first
integration check for image quality, local PP-OCR, local Qwen, and field
validation. After reviewing the four results, build the manually deidentified
OCR fixture dataset for the 50-sample deployment gate.
