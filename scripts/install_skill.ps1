param([string]$SkillHome)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
if (-not $SkillHome) {
    $taskCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $SkillHome = Join-Path $taskCodexRoot 'skills'
}
$taskSource = Join-Path $taskRoot 'skills/inkbridge-study'
$taskDestination = Join-Path $SkillHome 'inkbridge-study'
if (Test-Path -LiteralPath $taskDestination) {
    # Preserve an existing installation before replacing individual package files.
    $taskBackup = Join-Path (Split-Path $SkillHome -Parent) ('skill-backups/inkbridge-study-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ'))
    New-Item -ItemType Directory -Path (Split-Path $taskBackup -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $taskDestination -Destination $taskBackup -Recurse
}
New-Item -ItemType Directory -Path $taskDestination -Force | Out-Null
Copy-Item -Path (Join-Path $taskSource '*') -Destination $taskDestination -Recurse -Force
& python -X utf8 (Join-Path $taskDestination 'scripts/bridge.py') configure --root $taskRoot
if ($LASTEXITCODE -ne 0) { throw 'Skill installed but local root configuration failed.' }
Write-Output ('Installed skill: ' + $taskDestination)
