<#
.SYNOPSIS
  Verifies the Agentify development environment. Read-only; prints a readiness table.
#>
$ErrorActionPreference = 'Continue'
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
$env:WINAPP_CLI_TELEMETRY_OPTOUT = '1'

$rows = @()
function Add-Row($name, $required, $ok, $detail) {
    $script:rows += [pscustomobject]@{ Item = $name; Required = $required; Status = $(if ($ok) { 'OK' } else { 'MISSING' }); Detail = $detail }
}

# OS
$os = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion")
Add-Row 'Windows 11' $true ([int]$os.CurrentBuildNumber -ge 22000) "$($os.ProductName) $($os.DisplayVersion) build $($os.CurrentBuildNumber)"

# uv
try { $v = (uv --version 2>$null); Add-Row 'uv' $true ($null -ne $v) $v } catch { Add-Row 'uv' $true $false 'not on PATH' }

# Python 3.12 via py launcher
try { $v = (py -3.12 --version 2>$null); Add-Row 'Python 3.12' $true ($v -match '3\.12') $v } catch { Add-Row 'Python 3.12' $true $false 'py -3.12 failed' }

# WinApp CLI
try {
    $v = (winapp --version 2>$null | Select-Object -Last 1)
    $pinned = '0.7.1'
    Add-Row 'WinApp CLI' $true ($null -ne $v) "$v (pinned: $pinned$(if ($v -and $v.Trim() -ne $pinned) { ' - VERSION DIFFERS, re-verify command syntax' }))"
} catch { Add-Row 'WinApp CLI' $true $false 'winget install Microsoft.WinAppCli' }

# Claude Code CLI (Explorer sub-agent for explore_app)
try { $v = (claude --version 2>$null); Add-Row 'Claude Code CLI' $false ($null -ne $v) $v } catch { Add-Row 'Claude Code CLI' $false $false 'needed only for explore_app' }

# Display scaling
Add-Type -AssemblyName System.Windows.Forms
$scr = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
Add-Row 'Display' $false $true "$($scr.Width)x$($scr.Height) logical; WinApp CLI reports physical pixels and scale"

# Interactive desktop (RDP check)
Add-Row 'Interactive desktop (not RDP)' $true ($env:SESSIONNAME -notmatch 'RDP') "SESSIONNAME=$env:SESSIONNAME"

$rows | Format-Table -AutoSize -Wrap
$missing = $rows | Where-Object { $_.Required -and $_.Status -ne 'OK' }
if ($missing) { Write-Host "`nMissing required items: $($missing.Item -join ', ')" -ForegroundColor Yellow; exit 1 } else { Write-Host "`nEnvironment ready." -ForegroundColor Green }
