param(
    [string]$Python = "",
    [string]$OcrEndpoint = "http://127.0.0.1:5002/ocr",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8030,
    [ValidateSet("uie-base", "uie-medical-base")]
    [string]$Model = "uie-base",
    [ValidateSet("cpu", "gpu")]
    [string]$Device = "cpu"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $root ".venv-uie\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "UIE Python environment not found: $Python"
}

& $Python -m rk3588_report_parser.uie_web_server `
    --host $HostAddress `
    --port $Port `
    --model $Model `
    --device $Device `
    --ocr-endpoint $OcrEndpoint `
    --schema (Join-Path $root "runtime\active_uie_schema.json") `
    --result-file (Join-Path $root "runtime\latest_uie_patient.json")
exit $LASTEXITCODE
