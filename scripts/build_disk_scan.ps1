# Builds the native disk scanner (Rust/PyO3) and installs it as:
#
#   <repo>\jarvis_disk.pyd                      dev: `import jarvis_disk`
#   backend-dist\backend\app\jarvis_disk.pyd    packaged: shipped in the installer
#
# The packaged backend is assembled by scripts/build_backend.ps1; run this
# afterwards (or before, it only needs the folder to exist). CI builds a clean
# .pyd on a fresh machine, so no local build paths end up in the binary.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$crate = Join-Path $root "native\jarvis-disk"
$appDir = Join-Path $root "backend-dist\backend\app"
$dll = Join-Path $crate "target\release\jarvis_disk.dll"

Push-Location $crate
try {
    # cargo always writes its progress to stderr, and Windows PowerShell turns
    # native stderr into a terminating NativeCommandError while
    # $ErrorActionPreference is Stop - which aborts a build that actually
    # succeeded. Going through cmd.exe with 2>&1 keeps the real exit code and
    # leaves the log on stdout.
    $log = & cmd.exe /d /c "cargo build --release 2>&1"
    $code = $LASTEXITCODE
    $log | ForEach-Object { Write-Host $_ }
    if ($code -ne 0) { throw "cargo build failed (exit $code)" }
}
finally { Pop-Location }

if (-not (Test-Path -LiteralPath $dll)) { throw "expected $dll" }

# CPython loads .pyd; the crate targets abi3-py310, so one file works for 3.10+.
$pyd = Join-Path $root "jarvis_disk.pyd"
Copy-Item -LiteralPath $dll -Destination $pyd -Force
Write-Host "installed $pyd ($((Get-Item -LiteralPath $dll).Length) bytes)"

if (-not (Test-Path -LiteralPath $appDir)) {
    New-Item -ItemType Directory -Path $appDir -Force | Out-Null
    Write-Warning "backend-dist was empty - run scripts/build_backend.ps1 to stage the full backend"
}
$bundled = Join-Path $appDir "jarvis_disk.pyd"
Copy-Item -LiteralPath $dll -Destination $bundled -Force
Write-Host "bundled   $bundled ($((Get-Item -LiteralPath $dll).Length) bytes)"

# bundle.resources maps backend-dist\backend into $RESOURCE\backend. Fail loudly
# if that ever stops matching, otherwise the installer would ship without the
# native scanner and disk tools would only work on dev machines.
$confPath = Join-Path $root "src-tauri\tauri.conf.json"
$conf = Get-Content -LiteralPath $confPath -Raw
if ($conf -notmatch '"resources"') {
    throw "src-tauri/tauri.conf.json has no bundle.resources entry - the scanner would not ship"
}
if ($conf -notmatch 'backend-dist') {
    throw "src-tauri/tauri.conf.json bundle.resources no longer points at backend-dist/backend"
}
