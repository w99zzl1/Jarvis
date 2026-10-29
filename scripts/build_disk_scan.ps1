# Builds the native disk scanner and installs it as <repo>\jarvis_disk.pyd
# AND src-tauri\binaries\jarvis_disk.pyd, so the Python agent can
# `import jarvis_disk` in-process (no exe, no PATH) both in dev and from inside
# the packaged Tauri app (tauri.conf.json -> bundle.resources -> binaries/*).
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$crate = Join-Path $root "native\jarvis-disk"
$binaries = Join-Path $root "src-tauri\binaries"

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

    $dll = Join-Path $crate "target\release\jarvis_disk.dll"
    if (-not (Test-Path -LiteralPath $dll)) { throw "expected $dll" }

    # CPython loads .pyd; abi3 keeps it valid across Python versions.
    $pyd = Join-Path $root "jarvis_disk.pyd"
    Copy-Item -LiteralPath $dll -Destination $pyd -Force
    Write-Host "installed $pyd ($((Get-Item -LiteralPath $dll).Length) bytes)"

    # Same file into the Tauri resource folder, so `tauri build` bundles it into
    # the .msi/.exe and a clean machine gets it without a Rust toolchain.
    # Two installs of one module name in one process is not a problem: CPython
    # keys `sys.modules` by name, and load_scanner() prefers the first import.
    if (-not (Test-Path -LiteralPath $binaries)) {
        New-Item -ItemType Directory -Path $binaries -Force | Out-Null
    }
    $bundled = Join-Path $binaries "jarvis_disk.pyd"
    Copy-Item -LiteralPath $dll -Destination $bundled -Force
    Write-Host "bundled  $bundled ($((Get-Item -LiteralPath $dll).Length) bytes)"

    # `bundle.resources` in src-tauri/tauri.conf.json globs binaries/*, so the
    # file above is picked up as-is. Fail loudly if that ever stops matching.
    $conf = Get-Content -LiteralPath (Join-Path $root "src-tauri\tauri.conf.json") -Raw
    if ($conf -notmatch '"resources"') {
        throw "src-tauri/tauri.conf.json has no bundle.resources entry - the scanner would not ship"
    }
}
finally { Pop-Location }
