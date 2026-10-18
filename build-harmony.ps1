param([string]$StudioPath = 'D:\DevEco Studio', [switch]$Clean, [switch]$SyncOnly,
      [string]$ProjectPath)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
if (-not $ProjectPath) { $ProjectPath = Join-Path $taskRoot 'app' }
$taskProfile = Join-Path $ProjectPath 'build-profile.json5'
if (-not (Test-Path -LiteralPath $taskProfile)) {
    Copy-Item -LiteralPath (Join-Path $ProjectPath 'build-profile.example.json5') -Destination $taskProfile
    Write-Host 'Created local unsigned build profile. Configure automatic signing in DevEco Studio for device installation.'
}
$taskNode = Join-Path $StudioPath 'tools\node\node.exe'
$taskJavaHome = Join-Path $StudioPath 'jbr'
$taskHvigorHome = Join-Path $StudioPath 'tools\hvigor'
$taskHvigor = Join-Path $taskHvigorHome 'hvigor\bin\hvigor.js'
foreach ($taskRequired in @($taskNode, $taskHvigor, (Join-Path $taskJavaHome 'bin\java.exe'), (Join-Path $StudioPath 'sdk\default\openharmony\native\oh-uni-package.json'))) {
    if (-not (Test-Path -LiteralPath $taskRequired)) { throw "DevEco component missing: $taskRequired" }
}
# Map the IDE's bundled packages into Node's normal scoped-package layout.
$taskModuleRoot = Join-Path $taskRoot 'tools\deveco-build-node_modules'
$taskScope = Join-Path $taskModuleRoot '@ohos'
New-Item -ItemType Directory -Path $taskScope -Force | Out-Null
foreach ($taskPackage in @('hvigor', 'hvigor-ohos-plugin')) {
    $taskLink = Join-Path $taskScope $taskPackage
    $taskTarget = Join-Path $taskHvigorHome $taskPackage
    if (-not (Test-Path -LiteralPath $taskLink)) {
        New-Item -ItemType Junction -Path $taskLink -Target $taskTarget | Out-Null
    } elseif ((Get-Item -LiteralPath $taskLink).Target -notcontains $taskTarget) {
        throw "Unexpected existing package mapping: $taskLink"
    }
}
$env:JAVA_HOME = $taskJavaHome
$env:NODE_HOME = Split-Path $taskNode -Parent
$env:NODE_PATH = $taskModuleRoot
$env:DEVECO_SDK_HOME = Join-Path $StudioPath 'sdk'
$env:OHOS_BASE_SDK_HOME = Join-Path $StudioPath 'sdk\default\openharmony'
$env:PATH = $env:NODE_HOME + ';' + (Join-Path $taskJavaHome 'bin') + ';' + $env:PATH
$taskNativeTypes = Join-Path $ProjectPath 'entry\oh_modules\libink_native.so\index.d.ts'
if (-not (Test-Path -LiteralPath $taskNativeTypes)) {
    $taskOhpm = Join-Path $StudioPath 'tools\ohpm\bin\ohpm.bat'
    if (-not (Test-Path -LiteralPath $taskOhpm)) { throw 'Bundled OHPM is missing.' }
    Push-Location (Join-Path $ProjectPath 'entry')
    try {
        & $taskOhpm install
        if ($LASTEXITCODE -ne 0) { throw 'OHPM native type dependency installation failed.' }
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $taskNativeTypes)) { throw 'Native module index.d.ts is still missing after OHPM install.' }
}
$taskLogDir = Join-Path $taskRoot 'output\build'
New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $taskRoot 'output\packages') -Force | Out-Null
if (-not (Test-Path -LiteralPath $ProjectPath)) { $ProjectPath = Join-Path $taskRoot 'app' }
Push-Location $ProjectPath
try {
    if ($SyncOnly) {
        & $taskNode $taskHvigor --sync -p product=default --no-daemon 2>&1 |
            Tee-Object -FilePath (Join-Path $taskLogDir 'harmony-sync.log')
        exit $LASTEXITCODE
    }
    if ($Clean) {
        & $taskNode $taskHvigor clean -p product=default --no-daemon
        if ($LASTEXITCODE -ne 0) { throw 'HarmonyOS build cleanup failed.' }
    }
    & $taskNode $taskHvigor assembleHap --mode module -p product=default -p buildMode=debug --no-daemon 2>&1 |
        Tee-Object -FilePath (Join-Path $taskLogDir 'harmony-build.log')
    $taskExitCode = $LASTEXITCODE
} finally { Pop-Location }
if ($taskExitCode -ne 0) { exit $taskExitCode }
$taskSignedHap = Join-Path $ProjectPath 'entry\build\default\outputs\default\entry-default-signed.hap'
$taskUnsignedHap = Join-Path $ProjectPath 'entry\build\default\outputs\default\entry-default-unsigned.hap'
if (Test-Path -LiteralPath $taskSignedHap) {
    $taskHap = $taskSignedHap
    $taskPackageName = 'inkbridge-harmony-api24-signed.hap'
} elseif (Test-Path -LiteralPath $taskUnsignedHap) {
    $taskHap = $taskUnsignedHap
    $taskPackageName = 'inkbridge-harmony-api24-unsigned.hap'
} else { throw 'Expected HarmonyOS HAP is missing.' }
Copy-Item -LiteralPath $taskHap -Destination (Join-Path $taskRoot ('output\packages\' + $taskPackageName))
& python (Join-Path $taskRoot 'scripts\inspect_hap.py') $taskHap --report (Join-Path $taskLogDir 'harmony-build-report.json')
exit $LASTEXITCODE
