<#
  build_dist.ps1 - make a self-contained folder you can hand to someone.

  It produces:
    dist\pet-vocab\                 the thing to copy (folder)
    dist\pet-vocab-<yyyyMMdd>.zip   the thing to send (compress of the above)

  Inside: product sources + a portable CPython runtime (same version as the
  interpreter you build with) with the 4 pip packages already installed, so the
  recipient does not need Python at all.

  Usage
    powershell -NoProfile -ExecutionPolicy Bypass -File build_dist.ps1
    ... -Py "D:\Python313\python.exe"   # default: "python" from PATH
    ... -NoRuntime                      # sources only (recipient installs Python)
    ... -NoZip                          # build the folder, skip the zip
    ... -WithPacks                      # also copy vocab-tool\packs (existing libraries)
    ... -WithCache                      # also copy vocab-tool\cache (per-word TTS cache)
    ... -Name "vocabspeaker-win64"      # folder + zip name (default: pet-vocab)
    ... -Out "vocabspeaker\release"     # where to put it (default: dist)

  NOTE: this file is ASCII-only on purpose. Windows PowerShell 5.1 reads a
  BOM-less .ps1 as ANSI, so any CJK character here can break the quoting.
#>
param(
    [string]$Py = "python",
    [string]$Out = "dist",
    [string]$Name = "pet-vocab",
    [switch]$NoRuntime,
    [switch]$NoZip,
    [switch]$WithPacks,
    [switch]$WithCache
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }

$OutDir = Join-Path $Root $Out
$Stage = Join-Path $OutDir $Name

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

function Get-Size([string]$p) {
    if (-not (Test-Path $p)) { return 0 }
    if (Test-Path $p -PathType Leaf) { return (Get-Item $p).Length }
    $s = 0
    Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $s += $_.Length }
    return $s
}

# --- 0. product files (the only ones needed to run) -------------------------
$Prod = @(
    "vocab-tool\serve.py",
    "vocab-tool\webgen.py",
    "vocab-tool\build_vocab.py",
    "vocab-tool\home.html",
    "vocab-tool\generator.html",
    "vocab-tool\start_pet.bat",
    "vocab-tool\requirements.txt",
    "pet-player-sketch\index.html",
    "HOWTO.txt"
)
foreach ($f in $Prod) {
    if (-not (Test-Path (Join-Path $Root $f))) { Fail "missing file: $f" }
}

# --- 1. clean stage ---------------------------------------------------------
if (Test-Path $Stage) {
    Write-Host "removing old stage ..."
    Remove-Item $Stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# --- 2. copy sources --------------------------------------------------------
Write-Host "copying sources ..."
foreach ($f in $Prod) {
    $dst = Join-Path $Stage $f
    $dir = Split-Path -Parent $dst
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Copy-Item (Join-Path $Root $f) $dst -Force
}
foreach ($d in @("vocab-tool\packs", "vocab-tool\work", "vocab-tool\cache")) {
    $p = Join-Path $Stage $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}
if ($WithPacks) {
    Write-Host "copying existing libraries (packs) ..."
    $srcPacks = Join-Path $Root "vocab-tool\packs"
    if (Test-Path $srcPacks) {
        Copy-Item (Join-Path $srcPacks "*") (Join-Path $Stage "vocab-tool\packs") -Recurse -Force
    }
}
if ($WithCache) {
    Write-Host "copying per-word TTS cache ..."
    $srcCache = Join-Path $Root "vocab-tool\cache"
    if (Test-Path $srcCache) {
        Copy-Item (Join-Path $srcCache "*") (Join-Path $Stage "vocab-tool\cache") -Recurse -Force
    }
}

# --- 3. portable runtime ----------------------------------------------------
if (-not $NoRuntime) {
    Write-Host "probing interpreter: $Py"
    try {
        $base = (& $Py -c "import sys;print(sys.base_prefix)" 2>$null | Select-Object -First 1)
        $sp = (& $Py -c "import sysconfig;print(sysconfig.get_paths()['purelib'])" 2>$null | Select-Object -First 1)
    }
    catch {
        Fail "cannot run '$Py'. Pass -Py <full path to python.exe>"
    }
    if (-not $base) { Fail "cannot run '$Py'. Pass -Py <full path to python.exe>" }
    $base = $base.Trim()
    $sp = $sp.Trim()
    if (-not (Test-Path $base)) { Fail "python base prefix not found: $base" }
    if (-not (Test-Path $sp)) { Fail "site-packages not found: $sp" }
    Write-Host "  base           : $base"
    Write-Host "  site-packages  : $sp"

    $rt = Join-Path $Stage "runtime"
    $rtLib = Join-Path $rt "Lib"
    New-Item -ItemType Directory -Force -Path $rtLib | Out-Null

    # interpreter + runtime dlls
    foreach ($pat in @("python.exe", "pythonw.exe", "python3.dll", "python3*.dll",
                       "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")) {
        Get-ChildItem $base -Filter $pat -File -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item $_.FullName $rt -Force }
    }
    # native extension modules (_ssl, _socket, sqlite3, ...)
    $dlls = Join-Path $base "DLLs"
    if (-not (Test-Path $dlls)) { Fail "no DLLs directory in $base - is this a full CPython install?" }
    Copy-Item $dlls $rt -Recurse -Force

    # standard library, minus what nothing here imports
    $skip = @("site-packages", "test", "tests", "idlelib", "tkinter", "turtledemo",
              "ensurepip", "lib2to3", "pydoc_data")
    foreach ($item in Get-ChildItem (Join-Path $base "Lib")) {
        if ($skip -contains $item.Name) { continue }
        Copy-Item $item.FullName (Join-Path $rtLib $item.Name) -Recurse -Force
    }

    # only the packages build_vocab / webgen actually import, plus their deps
    $pkgs = @(
        "pymupdf", "fitz", "imageio_ffmpeg", "docx", "lxml", "edge_tts", "aiohttp",
        "aiosignal", "attrs", "attr", "frozenlist", "multidict", "yarl", "propcache",
        "aiohappyeyeballs", "certifi", "idna", "tabulate", "typing_extensions.py"
    )
    $dstSp = Join-Path $rtLib "site-packages"
    New-Item -ItemType Directory -Force -Path $dstSp | Out-Null
    foreach ($p in $pkgs) {
        $src = Join-Path $sp $p
        if (Test-Path $src) {
            Copy-Item $src $dstSp -Recurse -Force
        }
        else {
            Write-Host "  (not installed, skipped) $p" -ForegroundColor DarkYellow
        }
    }
    # keep dist-info so "pip list" / license info still works in the portable runtime
    $keepMeta = "^(pymupdf|imageio_ffmpeg|python_docx|lxml|edge_tts|aiohttp|aiosignal|attrs|frozenlist|multidict|yarl|propcache|aiohappyeyeballs|certifi|idna|tabulate|typing_extensions)-"
    Get-ChildItem $sp -Directory -Filter "*.dist-info" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.ToLower() -match $keepMeta } |
        ForEach-Object { Copy-Item $_.FullName $dstSp -Recurse -Force }

    # pymupdf ships C headers + a 3.8 MB import lib that are never used at runtime
    $pd = Join-Path $dstSp "pymupdf"
    foreach ($x in @("mupdf-devel", "__pycache__")) {
        $dead = Join-Path $pd $x
        if (Test-Path $dead) { Remove-Item $dead -Recurse -Force }
    }
    Get-ChildItem $dstSp -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force

    # A ._pth file puts the interpreter in isolated mode: sys.path comes ONLY
    # from this file, so the user's own %APPDATA%\Python\...\site-packages is
    # ignored. Without it, any machine that happens to have pymupdf installed
    # there loads THAT copy instead of the one we ship (happened while testing).
    $dllName = (Get-ChildItem $rt -Filter "python3*.dll" |
                Where-Object { $_.BaseName -match '^python\d{3}$' } |
                Select-Object -First 1).BaseName
    if (-not $dllName) { Fail "no pythonXY.dll found in the runtime folder" }
    # NOTE: "import site" is deliberately absent. With it, site.py appends the
    # user site-packages again (measured: C:\Users\<u>\AppData\Roaming\Python\
    # Python313\site-packages showed up on sys.path next to ours), which defeats
    # the whole point of the ._pth file. We ship no .pth files, so site
    # processing would buy us nothing here.
    $pthLines = @(".", "DLLs", "Lib", "Lib\site-packages")
    Set-Content -Path (Join-Path $rt ($dllName + "._pth")) -Value $pthLines -Encoding ASCII

    # --- self test: if this fails, do NOT ship the folder -------------------
    $rtPy = Join-Path $rt "python.exe"
    if (-not (Test-Path $rtPy)) { Fail "portable python.exe was not created" }
    $rtFull = (Resolve-Path $rt).Path.TrimEnd("\")
    Write-Host "self-test: importing deps with the portable interpreter ..."
    # native commands writing to stderr would abort the script under
    # $ErrorActionPreference=Stop, so judge by exit code instead
    $ErrorActionPreference = "Continue"
    $probe = & $rtPy -X utf8 -W ignore -c "import pymupdf, edge_tts, docx, imageio_ffmpeg, lxml, aiohttp, sys; print('IMPORT-OK'); [print('PATH=' + p) for p in sys.path]" 2>&1
    $probeRc = $LASTEXITCODE
    # ffmpeg must be reachable too - it is the one thing that fails silently later
    $probe2 = & $rtPy -X utf8 -W ignore -c "import imageio_ffmpeg,os;p=imageio_ffmpeg.get_ffmpeg_exe();print('FFMPEG-OK' if os.path.getsize(p)>1000000 else 'FFMPEG-BAD')" 2>&1
    $probe2Rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($probeRc -ne 0 -or (($probe -join "`n") -notmatch "IMPORT-OK")) {
        Write-Host ($probe -join "`n")
        Fail "portable runtime cannot import the dependencies (see output above)"
    }
    # EVERY sys.path entry must live inside runtime\ - not just "the first
    # site-packages looks right". The old first-match check stayed green while
    # the machine's own APPDATA site-packages was still on sys.path.
    $rtPaths = @($probe | Where-Object { "$_" -like "PATH=*" } | ForEach-Object { "$_".Substring(5) })
    if ($rtPaths.Count -eq 0) {
        Write-Host ($probe -join "`n")
        Fail "self-test printed no sys.path entries - cannot verify isolation"
    }
    $outside = @($rtPaths | Where-Object { -not $_.StartsWith($rtFull, [System.StringComparison]::OrdinalIgnoreCase) })
    if ($outside.Count -gt 0) {
        Write-Host ("sys.path: " + ($rtPaths -join "; "))
        Write-Host ("outside : " + ($outside -join "; "))
        Fail "portable runtime is loading modules from outside the package"
    }
    if ($probe2Rc -ne 0 -or (($probe2 -join "`n") -notmatch "FFMPEG-OK")) {
        Write-Host ($probe2 -join "`n")
        Fail "portable runtime cannot locate a usable ffmpeg binary"
    }

    # the product itself must start. Importing third-party libs says nothing
    # about that: isolated mode keeps the script's own folder OFF sys.path, so
    # serve.py's "import webgen" failed in the shipped folder while every probe
    # above was green. --help parses args and exits before binding a port.
    $stageServe = Join-Path $Stage "vocab-tool\serve.py"
    if (-not (Test-Path $stageServe)) { Fail "vocab-tool\serve.py is missing from the stage" }
    $ErrorActionPreference = "Continue"
    $smoke = & $rtPy -X utf8 $stageServe --help 2>&1
    $smokeRc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($smokeRc -ne 0 -or (($smoke -join "`n") -notmatch "usage")) {
        Write-Host ($smoke -join "`n")
        Fail "the packaged server does not start (serve.py --help failed above)"
    }
    Write-Host "  runtime ok" -ForegroundColor Green
}

# --- 4. zip -----------------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd"
$zip = Join-Path $OutDir ($Name + "-" + $stamp + ".zip")
if (-not $NoZip) {
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "zipping (this takes a minute for ~170 MB) ..."
    Compress-Archive -Path $Stage -DestinationPath $zip -CompressionLevel Optimal
}

# --- 5. report --------------------------------------------------------------
Write-Host ""
Write-Host ("folder : {0}   ({1} MB)" -f $Stage, [math]::Round((Get-Size $Stage) / 1MB, 1))
if (-not $NoZip) {
    Write-Host ("zip    : {0}   ({1} MB)" -f $zip, [math]::Round((Get-Size $zip) / 1MB, 1))
}
Write-Host "done."
