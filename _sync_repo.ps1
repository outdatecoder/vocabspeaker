<#
  _sync_repo.ps1 - keep the development copy and the publishable repo byte-identical.

  WHY: the project lives in two directories on the same disk:
      DEV  = <parent of this repo>\        (vocab-tool\, pet-player-sketch\, build_dist.ps1)
      REPO = this folder (vocabspeaker\)    (what git pushes, what others clone)
  Nothing enforced that they stayed in sync - it was "remember to copy". This script
  makes it checkable: no arguments = report only, -Sync = copy then re-check.

  DIRECTION MATTERS. Most files have DEV as the source of truth (the code is edited
  there). README.md is the opposite: it is the page a visitor sees on GitHub and it
  travels with the Releases, so it is edited in REPO and mirrored back to DEV.
  Do not flip a file between the two lists without saying why in the commit message.

  ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
  so any CJK character here can break the quoting (see AGENTS.md section 4).

  Usage
    powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1 -Sync

  Exit code: 0 = after this run every file on both lists is identical, 1 = differences
  remain (or a file is missing / empty). A gate can rely on the exit code.
#>
param(
    [switch]$Sync
)

$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }
$dev = Split-Path -Parent $repo
$devTool = Join-Path $dev 'vocab-tool'

function Fail([string]$msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $devTool 'serve.py'))) {
    Fail ("dev copy not found. This script must sit in <dev>\vocabspeaker\, and <dev>\vocab-tool\serve.py must exist.")
}

# dev -> repo. The code is edited in DEV; the repo is the copy that gets pushed.
$ToRepo = @(
    'build_dist.ps1',
    'HOWTO.txt',
    'LICENSE',
    'pet-player-sketch\index.html',
    'vocab-tool\serve.py',
    'vocab-tool\webgen.py',
    'vocab-tool\build_vocab.py',
    'vocab-tool\home.html',
    'vocab-tool\generator.html',
    'vocab-tool\start_pet.bat',
    'vocab-tool\requirements.txt',
    'vocab-tool\_word_pages.ps1',
    # gate scripts (see AGENTS.md section 5)
    'vocab-tool\_gateutil.py',
    'vocab-tool\_probe_pack.py',
    'vocab-tool\_cdp_probe.py',
    'vocab-tool\_cdp_inject.py',
    'vocab-tool\_cdp_ui.py',
    'vocab-tool\_cdp_home.py',
    'vocab-tool\_cdp_shot.py',
    'vocab-tool\_api_probe.py',
    'vocab-tool\_e2e_generate.py',
    'vocab-tool\_check_timeline.py',
    'vocab-tool\_trace.js',
    'vocab-tool\_trace_dl.js',
    'vocab-tool\_bench_word_com.ps1',
    'vocab-tool\_restart_serve.ps1'
)

# repo -> dev. README is the public front page: it is edited in REPO, mirrored to DEV.
$ToDev = @(
    'README.md'
)

function Get-Sha([string]$p) {
    if (-not (Test-Path $p -PathType Leaf)) { return 'MISSING' }
    return (Get-FileHash -Algorithm SHA256 -Path $p).Hash.Substring(0, 12)
}

$ok = 0
$diff = 0
$missing = 0
$synced = 0
$skipped = 0

Write-Host "dev  : $dev"
Write-Host "repo : $repo"
Write-Host "mode : $(if ($Sync) { 'SYNC (copy then verify)' } else { 'CHECK (read only)' })"
Write-Host ""

foreach ($item in @(
        @{ list = $ToRepo; srcRoot = $dev; dstRoot = $repo; label = 'dev -> repo' },
        @{ list = $ToDev; srcRoot = $repo; dstRoot = $dev; label = 'repo -> dev' }
    )) {

    foreach ($rel in $item.list) {
        $src = Join-Path $item.srcRoot $rel
        $dst = Join-Path $item.dstRoot $rel
        $hs = Get-Sha $src
        $hd = Get-Sha $dst

        if ($hs -eq 'MISSING') {
            Write-Host ("MISSING  {0}  (source: {1})" -f $rel, $item.label) -ForegroundColor Red
            $missing++
            continue
        }
        # Never let an empty source overwrite a real file: a 0-byte start_pet.bat
        # already sat in the docs folder for ten days and looked like a working launcher.
        if ((Get-Item $src).Length -eq 0) {
            Write-Host ("EMPTY    {0}  (source is 0 bytes, not copied)" -f $rel) -ForegroundColor Red
            $skipped++
            continue
        }
        if ($hs -eq $hd) {
            $ok++
            continue
        }

        if (-not $Sync) {
            Write-Host ("DIFF     {0}  {1} != {2}  ({3})" -f $rel, $hd, $hs, $item.label) -ForegroundColor Yellow
            $diff++
            continue
        }

        $dstDir = Split-Path -Parent $dst
        if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
        Copy-Item -Path $src -Destination $dst -Force
        $h2 = Get-Sha $dst
        if ($h2 -eq $hs) {
            Write-Host ("SYNC     {0}  {1} -> {2}  ({3})" -f $rel, $hd, $h2, $item.label) -ForegroundColor Cyan
            $synced++
        }
        else {
            Write-Host ("FAILED   {0}  copy did not verify ({1} != {2})" -f $rel, $h2, $hs) -ForegroundColor Red
            $diff++
        }
    }
}

# Repo-side extras: files in vocab-tool that no list mentions. Catches "added a new
# gate script but forgot to register it", which would otherwise drift invisibly.
$known = @{}
foreach ($rel in $ToRepo) { $known[(Split-Path -Leaf $rel)] = $true }
$repoTool = Join-Path $repo 'vocab-tool'
$extra = @(Get-ChildItem $repoTool -File -ErrorAction SilentlyContinue |
    Where-Object { -not $known.ContainsKey($_.Name) -and $_.Name -notmatch '^(local_python\.txt|word_pages\.json)$' } |
    ForEach-Object { $_.Name })
if ($extra.Count -gt 0) {
    Write-Host ("EXTRA    repo/vocab-tool has files not in the list: {0}" -f ($extra -join ', ')) -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("identical {0}   synced {1}   diff {2}   missing {3}   empty-skip {4}" -f $ok, $synced, $diff, $missing, $skipped)
if ($diff -gt 0 -or $missing -gt 0 -or $skipped -gt 0) {
    if (-not $Sync) { Write-Host "run with -Sync to copy the source side over." -ForegroundColor Yellow }
    exit 1
}
if ($synced -gt 0) {
    Write-Host "now run: git -C $repo status --short   (then commit the updated copies)" -ForegroundColor Yellow
}
Write-Host "all files identical" -ForegroundColor Green
exit 0
