param(
  [string]$Python = "python",
  [string]$Config = "",
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8020,
  [switch]$AllowUnverifiedRuntime
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $root "config.pc.example.json" }
$env:PYTHONPATH = Join-Path $root "src"
$arguments = @(
  "-m", "rk3588_report_parser.web_server",
  "--config", $Config,
  "--host", $HostAddress,
  "--port", $Port
)
if ($AllowUnverifiedRuntime) { $arguments += "--allow-unverified-runtime" }
& $Python @arguments
exit $LASTEXITCODE
