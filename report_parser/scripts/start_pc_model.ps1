[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ModelPath,

    [string]$Python = "python",

    [ValidateSet("auto", "cpu", "cuda", "cuda:0", "cuda:1")]
    [string]$Device = "auto",

    [int]$Port = 8010
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

& $Python -m rk3588_report_parser.desktop_server --model-path $ModelPath --host 127.0.0.1 --port $Port --device $Device
exit $LASTEXITCODE
