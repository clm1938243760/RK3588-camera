[CmdletBinding()]
param(
    [string]$VenvName = ".venv-uie"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = [System.IO.Path]::GetFullPath((Join-Path $root $VenvName))
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    & py -3.10 -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pip install -r (Join-Path $root "requirements-uie.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pip install --no-deps "paddlenlp==2.8.1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pip install -e $root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -c "import paddle, paddlenlp; from paddlenlp import Taskflow; print('paddle=' + paddle.__version__); print('paddlenlp=' + paddlenlp.__version__)"
exit $LASTEXITCODE
