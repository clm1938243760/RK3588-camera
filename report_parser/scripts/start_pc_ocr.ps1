[CmdletBinding()]
param(
    [string]$Python = "python",

    [int]$Port = 5002,

    [switch]$BootstrapModels,

    [string]$DetModelDir,

    [string]$RecModelDir,

    [string]$ClsModelDir,

    [switch]$DisableAngleCls
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$arguments = @("-m", "rk3588_report_parser.desktop_ppocr_server", "--host", "127.0.0.1", "--port", "$Port")
if ($BootstrapModels) {
    $arguments += "--bootstrap-models"
}
if ($DetModelDir) {
    $arguments += @("--det-model-dir", $DetModelDir)
}
if ($RecModelDir) {
    $arguments += @("--rec-model-dir", $RecModelDir)
}
if ($ClsModelDir) {
    $arguments += @("--cls-model-dir", $ClsModelDir)
}
if ($DisableAngleCls) {
    $arguments += "--disable-angle-cls"
}

& $Python @arguments
exit $LASTEXITCODE
