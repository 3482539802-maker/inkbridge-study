param(
    [ValidateRange(0,604800)][int]$Seconds = 0,
    [switch]$Demo,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = (Get-Command python -ErrorAction Stop).Source
if ($Demo) {
    $taskLogDir = Join-Path $taskRoot 'output\demo'
    New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null
    $taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $taskOutput = Join-Path $taskLogDir "synthetic-$taskStamp.jsonl"
    $taskReceiver = Start-Process -FilePath $taskPython -ArgumentList @(
        ('"' + (Join-Path $taskRoot 'scripts\ink_receiver.py') + '"'),
        '--seconds', $Seconds, '--output', ('"' + $taskOutput + '"')
    ) -PassThru -WindowStyle Hidden -WorkingDirectory $taskRoot `
      -RedirectStandardOutput (Join-Path $taskLogDir "$taskStamp.stdout.log") `
      -RedirectStandardError (Join-Path $taskLogDir "$taskStamp.stderr.log")
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 40; $taskAttempt++) {
        if ($taskReceiver.HasExited) { throw 'Receiver exited; inspect output\demo logs.' }
        try {
            $taskStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:18766/events?after=0' -TimeoutSec 1
            if ($taskStatus.records_path -eq $taskOutput) {
                $taskReady = $true
                break
            }
        } catch { Start-Sleep -Milliseconds 100 }
    }
    if (-not $taskReady) { Stop-Process -Id $taskReceiver.Id; throw 'Receiver did not become ready.' }
    Write-Host 'Demo: simulated input only. Receiver will stop automatically.'
    Write-Host "Receiver PID: $($taskReceiver.Id)"
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:18766' }
    & $taskPython (Join-Path $taskRoot 'scripts\simulate_ink.py')
    exit $LASTEXITCODE
}
Write-Host 'Viewer: http://127.0.0.1:18766'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:18766' }
Write-Host 'Press Ctrl+C to stop and save the report.'
& $taskPython (Join-Path $taskRoot 'scripts\ink_receiver.py') --seconds $Seconds
exit $LASTEXITCODE
