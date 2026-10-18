$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tablet_watch_selection.ps1')
$cases = @(
    @{ Name='remembered wireless with no USB'; Args=@{RememberedAddress='10.0.0.2:1234'}; Expected='10.0.0.2:1234' },
    @{ Name='new saved endpoint after disconnect'; Args=@{CurrentTarget='10.0.0.2:1234';RememberedAddress='10.0.0.3:1234'}; Expected='10.0.0.3:1234' },
    @{ Name='unique new connected endpoint'; Args=@{CurrentTarget='10.0.0.2:1234';ConnectedTargets=@('10.0.0.3:1234');RememberedAddress='10.0.0.2:1234'}; Expected='10.0.0.3:1234' },
    @{ Name='explicit target remains pinned'; Args=@{PinnedTarget='10.0.0.2:1234';ConnectedTargets=@('10.0.0.3:1234');RememberedAddress='10.0.0.3:1234'}; Expected='10.0.0.2:1234' },
    @{ Name='ambiguity refuses selection'; Args=@{ConnectedTargets=@('usb1','usb2');RememberedAddress='10.0.0.2:1234'}; Expected=$null },
    @{ Name='current connection preserved'; Args=@{CurrentTarget='usb1';ConnectedTargets=@('usb1','usb2');RememberedAddress='10.0.0.2:1234'}; Expected='usb1' },
    @{ Name='USB fallback'; Args=@{ConnectedTargets=@('usb1')}; Expected='usb1' }
)
foreach ($case in $cases) {
    $parameters = $case.Args
    $actual = Select-TabletWatchTarget @parameters
    if ($actual -ne $case.Expected) { throw "Failed: $($case.Name): $actual" }
}
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '..\watch-tablet.ps1'), [ref]$null, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
Write-Host "PASS: $($cases.Count) target selection cases and watcher syntax."
