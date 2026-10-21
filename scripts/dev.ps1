<#
.SYNOPSIS
  Developer task runner.
.EXAMPLE
  .\scripts\dev.ps1 check        # lint + format check + types + unit tests
  .\scripts\dev.ps1 integration  # real WinApp CLI tests (unlocked desktop required)
  .\scripts\dev.ps1 fixture -Version B   # launch the UI-change fixture app by hand
#>
param(
    [ValidateSet('check','fmt','unit','integration','all','doctor','fixture')][string]$Task = 'check',
    [ValidateSet('A','B')][string]$Version = 'A'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$env:WINAPP_CLI_TELEMETRY_OPTOUT = '1'
# Pick up PATH changes from winget installs without requiring a new terminal.
$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')

function Step($name, [scriptblock]$body) {
    Write-Host "== $name" -ForegroundColor Cyan
    & $body
    if ($LASTEXITCODE -ne 0) { throw "$name failed (exit $LASTEXITCODE)" }
}

switch ($Task) {
    'fmt'         { Step 'ruff format' { uv run ruff format . }; Step 'ruff fix' { uv run ruff check --fix . } }
    'unit'        { Step 'unit tests' { uv run pytest } }
    'integration' { Step 'integration tests' { uv run pytest -m integration -v } }
    'doctor'      { Step 'doctor' { uv run agentify doctor } }
    'fixture'     { powershell -NoProfile -ExecutionPolicy Bypass -File "$root\tests\fixtures\ui_change_fixture\fixture_app.ps1" -Version $Version }
    'check' {
        Step 'ruff check' { uv run ruff check . }
        Step 'ruff format --check' { uv run ruff format --check . }
        Step 'mypy' { uv run mypy }
        Step 'unit tests' { uv run pytest }
    }
    'all' {
        & $PSCommandPath -Task check
        & $PSCommandPath -Task integration
    }
}
