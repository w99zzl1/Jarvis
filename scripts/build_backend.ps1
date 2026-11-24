# Builds the Python backend that ships inside the installer:
#
#   backend-dist\backend\
#     python\          embeddable CPython (python.exe + DLLs + Lib\site-packages)
#     app\             server\, shared\, agents\, config\ (sources as-is)
#     app\jarvis_disk.pyd   native disk scanner (added by build_disk_scan.ps1)
#
# No PyInstaller: the embeddable distribution keeps the backend debuggable,
# starts in well under a second and does not get flagged as a packed binary.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_backend.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_backend.ps1 -Redownload
#
# Then: npm run tauri build   (bundle.resources maps backend/ into $RESOURCE)
param(
    [string]$PythonVersion = "3.10.10",
    [switch]$Redownload,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"

# =============================================================================
# SHA256 Checksums for supply chain security
# =============================================================================
# Python 3.10.10 embeddable amd64: https://www.python.org/ftp/python/3.10.10/python-3.10.10-embed-amd64.zip
# Generated: Get-FileHash python-3.10.10-embed-amd64.zip -Algorithm SHA256
$PythonEmbedChecksums = @{
    "3.10.10" = "B8C99A1BCE379287EAE580E7ECD0D8F4AFDCE0A27268D7F26DB205F3362D7BAB"
}
# get-pip.py: https://bootstrap.pypa.io/get-pip.py
# Generated: Get-FileHash get-pip.py -Algorithm SHA256
$GetPipChecksum = "356E0D0DE21CDC348C3394084D90939B85E4246C4D14C76AEE6CA4F46646EA98"

function Verify-Checksum {
    param([string]$FilePath, [string]$ExpectedChecksum, [string]$Label)
    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLower()
    $expected = $ExpectedChecksum.ToLower()
    if ($actual -ne $expected) {
        throw "$Label checksum mismatch!`n  Expected: $expected`n  Actual:   $actual`n  File:     $FilePath"
    }
    Write-Host "    ✓ $Label checksum verified"
}

$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root "backend-dist"
$stage = Join-Path $dist "backend"
$pyDir = Join-Path $stage "python"
$appDir = Join-Path $stage "app"
$cache = Join-Path $root ".cache"

function Invoke-Exe {
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    # Native stderr is a terminating error under $ErrorActionPreference=Stop in
    # Windows PowerShell 5.1, so keep the real exit code and route through cmd.
    $log = & cmd.exe /d /c "`"$Exe`" $($Arguments -join ' ') 2>&1"
    $code = $LASTEXITCODE
    $log | ForEach-Object { Write-Host "    $_" }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

Write-Host "==> Python $PythonVersion (embeddable, amd64)"
New-Item -ItemType Directory -Path $cache -Force | Out-Null

$zipName = "python-$PythonVersion-embed-amd64.zip"
$zip = Join-Path $cache $zipName
if ($Redownload -or -not (Test-Path -LiteralPath $zip)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Write-Host "    downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
}
# Verify Python embeddable checksum
if ($PythonEmbedChecksums.ContainsKey($PythonVersion)) {
    Verify-Checksum -FilePath $zip -ExpectedChecksum $PythonEmbedChecksums[$PythonVersion] -Label "Python $PythonVersion embeddable"
} else {
    Write-Warning "No checksum defined for Python $PythonVersion - skipping verification"
}

# Clean staging area. backend-dist is generated and git-ignored; keep the
# tracked README so the folder (and Tauri's resource path) still exists.
if (Test-Path -LiteralPath $stage) {
    Get-ChildItem -LiteralPath $stage -Force |
        Where-Object { $_.Name -ne "README.md" } |
        Remove-Item -Recurse -Force
} else {
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
}

Write-Host "==> unpacking interpreter"
New-Item -ItemType Directory -Path $pyDir -Force | Out-Null
Expand-Archive -LiteralPath $zip -DestinationPath $pyDir -Force

# The embeddable build starts isolated: the ._pth file defines sys.path and
# `import site` is commented out. Enable site-packages and make the backend
# sources importable no matter what the current directory is.
$pth = Get-ChildItem -LiteralPath $pyDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "python*._pth not found in $pyDir" }
$pthLines = @(
    "python310.zip",
    ".",
    "Lib\site-packages",
    "..\app",
    "import site"
)
Set-Content -LiteralPath $pth.FullName -Value $pthLines -Encoding ASCII
Write-Host "    patched $($pth.Name)"

$python = Join-Path $pyDir "python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "python.exe not found in $pyDir" }

Write-Host "==> bootstrapping pip"
$getPip = Join-Path $cache "get-pip.py"
if ($Redownload -or -not (Test-Path -LiteralPath $getPip)) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
}
# Verify get-pip.py checksum
Verify-Checksum -FilePath $getPip -ExpectedChecksum $GetPipChecksum -Label "get-pip.py"
Invoke-Exe -Exe $python -What "get-pip.py" -Arguments @(
    "`"$getPip`"", "--no-warn-script-location", "--disable-pip-version-check"
)

Write-Host "==> installing requirements.txt"
$requirements = Join-Path $root "requirements.txt"
if (-not (Test-Path -LiteralPath $requirements)) { throw "requirements.txt not found" }
Invoke-Exe -Exe $python -What "pip install" -Arguments @(
    "-m", "pip", "install",
    "--no-warn-script-location", "--disable-pip-version-check", "--no-cache-dir",
    "-r", "`"$requirements`""
)

Write-Host "==> copying backend sources"
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
foreach ($name in @("server", "shared", "agents", "config")) {
    $src = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $src)) { throw "missing source folder: $src" }
    Copy-Item -LiteralPath $src -Destination $appDir -Recurse -Force
}

# Never ship caches or the developer's byte-code.
Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $appDir -Recurse -File |
    Where-Object { $_.Extension -in ".pyc", ".pyo" } |
    Remove-Item -Force

# The native scanner is optional at this point: build_disk_scan.ps1 drops it
# into the staged app folder. Copy a locally built one if it already exists so
# a plain build_backend run still produces a complete, runnable backend.
$pydSources = @(
    (Join-Path $root "jarvis_disk.pyd"),
    (Join-Path $root "native\jarvis-disk\target\release\jarvis_disk.dll")
)
$pyd = $pydSources | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($pyd) {
    Copy-Item -LiteralPath $pyd -Destination (Join-Path $appDir "jarvis_disk.pyd") -Force
    Write-Host "==> bundled jarvis_disk.pyd from $pyd"
} else {
    Write-Warning "jarvis_disk.pyd not found - run scripts/build_disk_scan.ps1 before packaging"
}

if (-not $SkipSmokeTest) {
    Write-Host "==> smoke test (import every third-party module the backend uses)"
    $smokeFile = Join-Path $env:TEMP "jarvis_backend_smoke.py"
    @(
        "import fastapi, uvicorn, pydantic, httpx, requests, openai, groq",
        "import google.genai, bs4, ddgs, multipart",
        "import server.main",
        "print('backend imports: OK')"
    ) | Set-Content -LiteralPath $smokeFile -Encoding ASCII

    $smokeData = Join-Path $env:TEMP ("jarvis-smoke-" + [guid]::NewGuid().ToString("N"))
    $env:JARVIS_DATA_DIR = $smokeData
    try {
        Invoke-Exe -Exe $python -What "smoke test" -Arguments @("`"$smokeFile`"")
    }
    finally {
        Remove-Item Env:JARVIS_DATA_DIR -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $smokeData) {
            Remove-Item -LiteralPath $smokeData -Recurse -Force
        }
        Remove-Item -LiteralPath $smokeFile -ErrorAction SilentlyContinue
    }
}

$size = (Get-ChildItem -LiteralPath $stage -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host ("==> done: {0} ({1:N1} MB)" -f $stage, ($size / 1MB))
Write-Host "    next: scripts\build_disk_scan.ps1 (if the scanner is missing), then npm run tauri build"
