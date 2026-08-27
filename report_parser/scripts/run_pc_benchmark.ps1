[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ModelPath,

    [Parameter(Mandatory = $true)]
    [string]$Dataset,

    [string]$Python = "python",

    [string]$Config,

    [string]$Output,

    [ValidateSet("auto", "cpu", "cuda", "cuda:0", "cuda:1")]
    [string]$Device = "cuda:0",

    [ValidateSet("chat", "constrained_choice", "evidence_choice", "evidence_chat")]
    [string]$LinkerMode = "constrained_choice",

    [ValidateSet("model_only", "hybrid", "evidence")]
    [string]$AssociationMode = "model_only",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8010,

    [switch]$FailOnMismatch,

    [switch]$EnforceDeploymentTargets
)

$ErrorActionPreference = "Stop"

function Resolve-ExistingPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $PathValue)) {
        throw "$Name does not exist: $PathValue"
    }
    return (Resolve-Path -LiteralPath $PathValue).Path
}

function Quote-Argument {
    param([Parameter(Mandatory = $true)][string]$Value)

    # All values here are local paths or fixed flags.  The explicit quotes keep
    # Windows paths containing spaces intact when Start-Process builds argv.
    return '"' + $Value.Replace('"', '\"') + '"'
}

$root = Split-Path -Parent $PSScriptRoot
$model = Resolve-ExistingPath -PathValue $ModelPath -Name "Model path"
$datasetPath = Resolve-ExistingPath -PathValue $Dataset -Name "Dataset"
if ([string]::IsNullOrWhiteSpace($Config)) {
    $configPath = Join-Path $root "config.pc.example.json"
} else {
    $configPath = Resolve-ExistingPath -PathValue $Config -Name "Config"
}

if (Test-Path -LiteralPath $Python) {
    $pythonCommand = (Resolve-Path -LiteralPath $Python).Path
} else {
    $pythonCommand = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $Output = Join-Path $root "evaluation_output\desktop_benchmark_$timestamp.json"
}
$outputDirectory = Split-Path -Parent $Output
if ([string]::IsNullOrWhiteSpace($outputDirectory)) {
    $outputDirectory = (Get-Location).Path
    $Output = Join-Path $outputDirectory $Output
}
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputStem = [System.IO.Path]::GetFileNameWithoutExtension($outputPath)
$serverStdout = Join-Path $outputDirectory "$outputStem.server.stdout.log"
$serverStderr = Join-Path $outputDirectory "$outputStem.server.stderr.log"

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    throw "Port $Port is already in use. Stop the existing local model service or choose another port."
}

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $root "src"
$serverProcess = $null
$exitCode = 2

try {
    $serverArguments = @(
        "-m",
        "rk3588_report_parser.desktop_server",
        "--model-path",
        (Quote-Argument $model),
        "--host",
        "127.0.0.1",
        "--port",
        "$Port",
        "--device",
        "$Device"
    ) -join " "
    $startProcessParameters = @{
        FilePath = $pythonCommand
        ArgumentList = $serverArguments
        WorkingDirectory = $root
        WindowStyle = "Hidden"
        RedirectStandardOutput = $serverStdout
        RedirectStandardError = $serverStderr
        PassThru = $true
    }
    $serverProcess = Start-Process @startProcessParameters

    $healthUri = "http://127.0.0.1:$Port/health"
    $deadline = (Get-Date).AddSeconds(45)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 2
            if ($health.ok -eq $true) {
                $ready = $true
                break
            }
        } catch {
            # The local model is still loading. No OCR or report text is sent.
        }
        if ($serverProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        $errorTail = Get-Content -LiteralPath $serverStderr -Tail 20 -ErrorAction SilentlyContinue
        throw "Desktop model service did not become healthy. $($errorTail -join ' ')"
    }

    $evaluationArguments = @(
        "-m",
        "rk3588_report_parser.evaluation",
        "--config",
        $configPath,
        "--dataset",
        $datasetPath,
        "--llm-endpoint",
        "http://127.0.0.1:$Port/v1/chat/completions",
        "--linker-mode",
        $LinkerMode,
        "--association-mode",
        $AssociationMode,
        "--output",
        $outputPath
    )
    if ($FailOnMismatch) {
        $evaluationArguments += "--fail-on-mismatch"
    }
    if ($EnforceDeploymentTargets) {
        $evaluationArguments += "--enforce-deployment-targets"
    }

    & $pythonCommand @evaluationArguments
    $exitCode = $LASTEXITCODE
    Write-Host "Benchmark result: $outputPath"
} finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
        $serverProcess.WaitForExit(5000) | Out-Null
    }
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}

exit $exitCode
