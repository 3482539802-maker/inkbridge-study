function Select-TabletWatchTarget {
    param([string]$PinnedTarget, [string]$CurrentTarget,
          [string[]]$ConnectedTargets = @(), [string]$RememberedAddress)
    # Explicit targets never migrate to another device. Automatic mode follows
    # a unique connected target, then the latest verified local endpoint.
    if ($PinnedTarget) { return $PinnedTarget }
    if ($CurrentTarget -and $CurrentTarget -in $ConnectedTargets) { return $CurrentTarget }
    if ($ConnectedTargets.Count -eq 1) { return $ConnectedTargets[0] }
    if ($ConnectedTargets.Count -gt 1) { return $null }
    if ($RememberedAddress) { return $RememberedAddress }
    return $CurrentTarget
}
