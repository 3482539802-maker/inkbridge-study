param([string]$StudioPath = 'D:\DevEco Studio')
$ErrorActionPreference = 'Stop'
$taskLauncher = Join-Path $StudioPath 'bin\devecostudio64.exe'
if (-not (Test-Path -LiteralPath $taskLauncher)) { throw "DevEco Studio not found: $taskLauncher" }
$taskProject = Join-Path $PSScriptRoot 'app'
$taskAlias = 'D:\InkBridgeApp'
if (-not (Test-Path -LiteralPath $taskAlias)) {
    New-Item -ItemType Junction -Path $taskAlias -Target $taskProject | Out-Null
}
if ((Get-Item -LiteralPath $taskAlias).Target -notcontains $taskProject) {
    throw "Unexpected existing project alias: $taskAlias"
}
$taskProject = $taskAlias
$taskDataRoot = 'D:\DevEcoData\6.1'
New-Item -ItemType Directory -Path $taskDataRoot -Force | Out-Null
$taskVmOptions = Join-Path $taskDataRoot 'devecostudio.vmoptions'
$taskOptions = Get-Content -LiteralPath (Join-Path $StudioPath 'bin\devecostudio64.exe.vmoptions')
$taskOptions += @('-Didea.config.path=D:/DevEcoData/6.1/config',
                  '-Didea.system.path=D:/DevEcoData/6.1/system',
                  '-Didea.log.path=D:/DevEcoData/6.1/log',
                  '-Didea.plugins.path=D:/DevEcoData/6.1/plugins',
                  '-Doshi.os.windows.hkeyperfdata=false')
$taskOptions | Set-Content -LiteralPath $taskVmOptions -Encoding utf8NoBOM
$env:DEVECOSTUDIO_VM_OPTIONS = $taskVmOptions
# This is the interactive IDE used for account login and device signing.
Start-Process -FilePath $taskLauncher -ArgumentList ('"' + $taskProject + '"') -WindowStyle Normal
