<#
.SYNOPSIS
  Build the UI-change fixture as a standalone AgentifyFixture.exe with the csc.exe that ships with Windows.
  Output: tests/fixtures/ui_change_fixture/bin/AgentifyFixture.exe (gitignored).
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$src = Join-Path $root 'tests\fixtures\ui_change_fixture\fixture_app.cs'
$outDir = Join-Path $root 'tests\fixtures\ui_change_fixture\bin'
New-Item -ItemType Directory -Force $outDir | Out-Null
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "csc.exe not found at $csc" }
& $csc /nologo /target:winexe /optimize+ /out:"$outDir\AgentifyFixture.exe" /r:System.Windows.Forms.dll /r:System.Drawing.dll $src
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
& $csc /nologo /target:winexe /optimize+ /define:CRM_DEFAULT /out:"$outDir\AgentifyCRM.exe" /r:System.Windows.Forms.dll /r:System.Drawing.dll $src
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
Write-Host "built $outDir\AgentifyFixture.exe and $outDir\AgentifyCRM.exe"
