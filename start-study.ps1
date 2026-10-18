param([switch]$Status, [switch]$RegisterAtLogon)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskConfig = Join-Path $taskRoot 'output/learning/runtime.json'
if (!(Test-Path -LiteralPath $taskConfig)) { throw 'Prepare output/learning/runtime.json first; see docs/COURSE_AUTOMATION.md.' }
$taskSettings = Get-Content -LiteralPath $taskConfig -Raw | ConvertFrom-Json
$taskPython = $taskSettings.python
if (!$taskPython) { $taskPython = (Get-Command python).Source }
$taskWindowlessPython = Join-Path (Split-Path -Parent $taskPython) 'pythonw.exe'
if (Test-Path -LiteralPath $taskWindowlessPython) { $taskPython = $taskWindowlessPython }
if ($Status) {
    Invoke-RestMethod "http://127.0.0.1:$($taskSettings.port)/status.json" | ConvertTo-Json -Depth 8
    return
}
if ($RegisterAtLogon) {
    # Current user's interactive logon only; no password, elevation or tablet UI.
    $taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument '-X utf8 scripts/course_runtime.py --config output/learning/runtime.json' -WorkingDirectory $taskRoot
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $taskOptions = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'InkBridge-Learning-Local' -Action $taskAction -Trigger $taskTrigger -Settings $taskOptions -Principal $taskPrincipal -Description 'Local learning dashboard and capture service recovery. Never starts tablet recording or approves screen capture.' -Force | Select-Object TaskName,State
}
$taskRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'scripts[\\/]course_runtime\.py' -and $_.CommandLine -match '--config output[\\/]learning[\\/]runtime\.json' })
if (!$taskRunning.Count) {
    $taskLogRoot = Join-Path $taskRoot 'output/learning/logs'
    New-Item -ItemType Directory -Path $taskLogRoot -Force | Out-Null
    $taskStamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-X','utf8','scripts/course_runtime.py','--config','output/learning/runtime.json') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogRoot "runtime-$taskStamp.stdout.log") -RedirectStandardError (Join-Path $taskLogRoot "runtime-$taskStamp.stderr.log") -PassThru
    @{ pid=$taskProcess.Id; url="http://127.0.0.1:$($taskSettings.port)/"; tablet_ui_actions=@() } | ConvertTo-Json
} else {
    @{ pid=@($taskRunning.ProcessId); already_running=$true; url="http://127.0.0.1:$($taskSettings.port)/" } | ConvertTo-Json
}
