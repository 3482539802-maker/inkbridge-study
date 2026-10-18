param([string]$Target, [string]$WirelessAddress, [string]$HdcServer,
      [string]$StudioPath = 'D:\DevEco Studio')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\tablet_watch_selection.ps1')
$taskHdc = Join-Path $PSScriptRoot 'tools\hdc\hdc.exe'
if (-not (Test-Path -LiteralPath $taskHdc)) {
    $taskHdc = Join-Path $StudioPath 'sdk\default\openharmony\toolchains\hdc.exe'
}
if (-not (Test-Path -LiteralPath $taskHdc)) { throw 'HDC not found.' }
$taskServerArgs = @()
if ($HdcServer) { $taskServerArgs = @('-s', $HdcServer) }
if ($WirelessAddress) {
    if ($Target -and $Target -ne $WirelessAddress) { throw 'Target and WirelessAddress differ.' }
    $Target = $WirelessAddress
}
$taskPinnedTarget = $Target
$taskRememberedPath = Join-Path $PSScriptRoot 'output\device-connection.json'
$taskVerifiedTarget = ''
$taskLastStatus = ''
Write-Host 'Watching the HDC reverse route. App will not be relaunched. Ctrl+C stops this watcher.'
while ($true) {
    try {
        $taskListing = @(& $taskHdc @taskServerArgs list targets -v)
        if ($LASTEXITCODE -ne 0) { throw 'HDC server query failed.' }
        $taskTargets = @($taskListing | ForEach-Object {
            $taskColumns = $_.Trim() -split '\s+'
            if ($taskColumns.Count -ge 3 -and $taskColumns[1] -in @('USB','TCP') -and $taskColumns[2] -eq 'Connected') {
                $taskColumns[0]
            }
        })
        $taskRememberedAddress = ''
        if (-not $taskPinnedTarget -and -not $HdcServer -and (Test-Path -LiteralPath $taskRememberedPath)) {
            try {
                $taskRememberedAddress = [string](Get-Content -LiteralPath $taskRememberedPath -Raw | ConvertFrom-Json).wireless_address
            } catch { $taskRememberedAddress = '' }
        }
        $Target = Select-TabletWatchTarget -PinnedTarget $taskPinnedTarget -CurrentTarget $Target -ConnectedTargets $taskTargets -RememberedAddress $taskRememberedAddress
        if ($Target -and $Target -in $taskTargets) {
            $taskRoutes = @(& $taskHdc @taskServerArgs -t $Target fport ls)
            if ($LASTEXITCODE -ne 0) { throw 'Cannot query reverse routes.' }
            if ($taskVerifiedTarget -ne $Target -or -not ($taskRoutes | Where-Object { $_ -match 'tcp:18765\s+tcp:18765' -and $_ -match '\[Reverse\]' -and $_ -match [regex]::Escape($Target) })) {
                & (Join-Path $PSScriptRoot 'connect-tablet.ps1') -Target $Target -HdcServer $HdcServer -StudioPath $StudioPath -NoLaunch
                $taskVerifiedTarget = $Target
            }
            $taskStatus = "Reverse route ready: $Target"
        } elseif ($Target -match '^\d{1,3}(\.\d{1,3}){3}:\d{1,5}$') {
            $taskVerifiedTarget = ''
            & (Join-Path $PSScriptRoot 'connect-tablet.ps1') -WirelessAddress $Target -HdcServer $HdcServer -StudioPath $StudioPath -NoLaunch
            $taskStatus = "Wireless route restored: $Target"
        } else {
            $taskVerifiedTarget = ''
            $taskStatus = if ($taskTargets.Count -gt 1 -and -not $taskPinnedTarget) { 'Multiple connected targets; specify -Target to avoid selecting the wrong tablet.' } else { 'Waiting for tablet connection. Local capture continues if the app session is still active.' }
        }
    } catch {
        $taskStatus = "Waiting to restore route: $($_.Exception.Message)"
    }
    if ($taskStatus -ne $taskLastStatus) { Write-Host $taskStatus; $taskLastStatus = $taskStatus }
    Start-Sleep -Seconds 3
}
