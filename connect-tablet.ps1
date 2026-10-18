param(
    [string]$Target,
    [string]$StudioPath = 'D:\DevEco Studio',
    [switch]$Install,
    [string]$WirelessAddress,
    [string]$HdcServer,
    [switch]$CheckOnly,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskHdc = Join-Path $taskRoot 'tools\hdc\hdc.exe'
if (-not (Test-Path -LiteralPath $taskHdc)) {
    $taskHdc = Join-Path $StudioPath 'sdk\default\openharmony\toolchains\hdc.exe'
}
if (-not (Test-Path -LiteralPath $taskHdc)) { throw 'HDC not found; install DevEco Studio or restore tools\hdc.' }
$taskRememberedPath = Join-Path $taskRoot 'output\device-connection.json'
# Prefer an already connected target. Only reconnect the saved wireless endpoint
# when no target is connected; never start a tablet Ability as part of discovery.
if (-not $WirelessAddress -and -not $Target -and -not $HdcServer -and (Test-Path -LiteralPath $taskRememberedPath)) {
    $taskConnectedBefore = @(& $taskHdc list targets -v) | Where-Object {
        $taskCols = $_.Trim() -split '\s+'
        $taskCols.Count -ge 3 -and $taskCols[1] -in @('USB','TCP') -and $taskCols[2] -eq 'Connected'
    }
    if (-not $taskConnectedBefore) {
        try {
            $taskRemembered = Get-Content -LiteralPath $taskRememberedPath -Raw | ConvertFrom-Json
            $WirelessAddress = [string]$taskRemembered.wireless_address
        } catch { Write-Warning 'Saved tablet endpoint is invalid; specify -WirelessAddress.' }
    }
}
$taskServerArgs = @()
foreach ($taskEndpoint in @($WirelessAddress, $HdcServer)) {
    if (-not $taskEndpoint) { continue }
    $taskParsedIp = $null
    if ($taskEndpoint -notmatch '^([^:]+):(\d{1,5})$' -or
        -not [System.Net.IPAddress]::TryParse($Matches[1], [ref]$taskParsedIp) -or
        $taskParsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        [int]$Matches[2] -lt 1 -or [int]$Matches[2] -gt 65535) {
        throw 'Use an IPv4:port endpoint, taking the wireless port from the tablet settings.'
    }
}
if ($HdcServer) { $taskServerArgs = @('-s', $HdcServer) }
if ($CheckOnly -and $Install) { throw '-CheckOnly cannot install a package.' }
if ($WirelessAddress) {
    if ($Target -and $Target -ne $WirelessAddress) { throw '-Target conflicts with -WirelessAddress.' }
    # Diagnose the transport before HDC: an Offline entry can still cause tconn
    # to print "Target is connected". Remote servers must test from their side.
    if (-not $HdcServer) {
        $taskEndpointParts = $WirelessAddress.Split(':');
        $taskSocket = [System.Net.Sockets.TcpClient]::new()
        try {
            $taskConnect = $taskSocket.ConnectAsync($taskEndpointParts[0], [int]$taskEndpointParts[1])
            if (-not $taskConnect.Wait(3000) -or -not $taskSocket.Connected) {
                throw 'TCP connection timed out after 3000 ms.'
            }
        } catch {
            throw "Wireless TCP endpoint $WirelessAddress is unreachable. Check the tablet's current wireless-debugging address, same-network access or VPN/relay route. Details: $($_.Exception.Message)"
        } finally { $taskSocket.Dispose() }
    }
    $taskBeforeConnect = @(& $taskHdc @taskServerArgs list targets -v)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot query the selected HDC server.' }
    $taskOfflineEntry = $taskBeforeConnect | Where-Object {
        $taskParts = $_.Trim() -split '\s+'
        $taskParts.Count -ge 3 -and $taskParts[0] -eq $WirelessAddress -and $taskParts[2] -eq 'Offline'
    }
    if ($taskOfflineEntry) {
        & $taskHdc @taskServerArgs tconn $WirelessAddress -remove | Write-Host
        if ($LASTEXITCODE -ne 0) { throw 'Cannot remove the stale wireless HDC connection.' }
    }
    & $taskHdc @taskServerArgs tconn $WirelessAddress | Write-Host
    if ($LASTEXITCODE -ne 0) { throw 'HDC TCP connection failed.' }
    $Target = $WirelessAddress
    # tconn initiates connection; wait briefly for the authenticated state.
    for ($taskAttempt = 0; $taskAttempt -lt 8; $taskAttempt++) {
        $taskConnectionListing = @(& $taskHdc @taskServerArgs list targets -v)
        $taskReady = $taskConnectionListing | Where-Object {
            $taskParts = $_.Trim() -split '\s+'
            $taskParts.Count -ge 3 -and $taskParts[0] -eq $Target -and $taskParts[2] -eq 'Connected'
        }
        if ($taskReady) { break }
        if ($taskAttempt -lt 7) { Start-Sleep -Milliseconds 500 }
    }
}
$taskListing = @(& $taskHdc @taskServerArgs list targets -v)
if ($LASTEXITCODE -ne 0) { throw 'Cannot query the selected HDC server.' }
$taskTargets = @($taskListing | ForEach-Object {
    $taskColumns = $_.Trim() -split '\s+'
    if ($taskColumns.Count -ge 3 -and $taskColumns[1] -in @('USB','TCP') -and $taskColumns[2] -eq 'Connected') {
        $taskColumns[0]
    }
})
if (-not $Target) {
    if ($taskTargets.Count -eq 0) { throw 'No tablet connected. Connect USB, or use -WirelessAddress with the IP:port shown under wireless debugging.' }
    if ($taskTargets.Count -ne 1) { throw 'Specify -Target after checking hdc list targets; expected one device.' }
    $Target = $taskTargets[0]
}
if ($Target -notin $taskTargets) { throw 'Target is not connected. Check wireless debugging, network and tablet authorization.' }
if (-not $HdcServer -and $Target -match '^\d{1,3}(\.\d{1,3}){3}:\d{1,5}$') {
    New-Item -ItemType Directory -Force -Path (Split-Path $taskRememberedPath) | Out-Null
    $taskRememberedTemporary = $taskRememberedPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    @{ wireless_address = $Target; verified_at = [DateTimeOffset]::UtcNow.ToString('o'); automatic_launch = $false } |
        ConvertTo-Json | Set-Content -LiteralPath $taskRememberedTemporary -Encoding utf8
    Move-Item -LiteralPath $taskRememberedTemporary -Destination $taskRememberedPath -Force
}
if ($CheckOnly) {
    & $taskHdc @taskServerArgs -t $Target shell param get const.ohos.apiversion
    if ($LASTEXITCODE -ne 0) { throw 'Target did not respond to the API-level query.' }
    & $taskHdc @taskServerArgs -t $Target fport ls
    Write-Host "Connected: $Target. Read-only device check; app not launched."
    exit 0
}
$taskRoutes = @(& $taskHdc @taskServerArgs -t $Target fport ls)
if (-not ($taskRoutes | Where-Object { $_ -match 'tcp:18765\s+tcp:18765' -and $_ -match '\[Reverse\]' -and $_ -match [regex]::Escape($Target) })) {
    $taskForward = & $taskHdc @taskServerArgs -t $Target rport tcp:18765 tcp:18765 2>&1
    $taskForward | Write-Host
    if ($LASTEXITCODE -ne 0 -or ($taskForward -join "`n") -match '\[Fail\]') { throw 'HDC reverse forwarding failed.' }
}
if ($Install) {
    $taskHap = Join-Path $taskRoot 'output\packages\inkbridge-harmony-api24-signed.hap'
    if (-not (Test-Path -LiteralPath $taskHap)) { throw 'Build and sign the HAP before installing.' }
    $taskResult = & $taskHdc @taskServerArgs -t $Target install -r $taskHap 2>&1
    $taskResult | Write-Host
    if ($LASTEXITCODE -ne 0 -or ($taskResult -join "`n") -notmatch 'install bundle successfully') { throw 'Device installation did not report success.' }
}
if (-not $NoLaunch) {
    $taskLaunch = & $taskHdc @taskServerArgs -t $Target shell aa start -a EntryAbility -b com.example.inkbridge 2>&1
    $taskLaunch | Write-Host
    if ($LASTEXITCODE -ne 0 -or ($taskLaunch -join "`n") -notmatch 'start ability successfully') {
        throw 'App launch failed. Unlock the tablet and retry; keep debugging enabled.'
    }
}
Write-Host "Device: $Target. Reverse route ready; confirm screen authorization in the app to start a new recording."
if ($HdcServer) { Write-Host 'rport connects to the HDC server computer. Run the receiver there, or provide a reverse tunnel to this PC.' }
