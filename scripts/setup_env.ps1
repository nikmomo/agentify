<#
.SYNOPSIS
  Installs the Agentify toolchain with winget: uv, Python 3.12, Microsoft WinApp CLI.
  Idempotent; safe to re-run. Does not install any project code.
#>
$ErrorActionPreference = 'Stop'
$packages = @(
    @{ Id = 'astral-sh.uv';         Name = 'uv' },
    @{ Id = 'Python.Python.3.12';   Name = 'Python 3.12' },
    @{ Id = 'Microsoft.WinAppCli';  Name = 'Microsoft WinApp CLI' }
)
foreach ($p in $packages) {
    Write-Host "== $($p.Name) ($($p.Id))" -ForegroundColor Cyan
    winget install --id $p.Id --exact --silent --accept-source-agreements --accept-package-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # -1978335189 = already installed / no applicable upgrade
        Write-Warning "winget exited with $LASTEXITCODE for $($p.Id)"
    }
}
Write-Host "`nRestart the terminal (PATH changed), then run: uv sync ; .\scripts\check_env.ps1" -ForegroundColor Green
