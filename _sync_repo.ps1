<#
  _sync_repo.ps1 - keep the publishable repo a faithful mirror of the project.

  WHY: the project lives in two places on this disk, plus the docs live on another:
      DEV   = <parent of this repo>\        vocab-tool\, pet-player-sketch\, build_dist.ps1, AGENTS.md
      REPO  = this folder (vocabspeaker\)    what git pushes, what others clone
      DOCS  = -DocsRoot (default: the IDE workspace folder, another drive)
  Nothing enforced that they stayed in sync - it was "remember to copy".

  No arguments = report only (exit 1 when anything differs). -Sync = copy then re-verify.

  DIRECTION MATTERS. Three groups, three directions:
    1. code  DEV -> REPO     (the code is edited in DEV)
    2. README REPO -> DEV    (it is the public front page and ships with Releases)
    3. docs  DOCS -> REPO\docs  (long-form docs are edited in the IDE workspace;
              a mirror in the repo means a dead disk does not take the design
              history with it). AGENTS.md comes from DEV (group 1) because it is
              edited next to the code; its .learnings\ neighbours come from DOCS.
  Do not flip a file between groups without saying why in the commit message.

  ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so
  any CJK character here can break the quoting (see AGENTS.md section 4). The doc
  file names are therefore discovered with a *.md wildcard, never spelled out.

  Usage
    powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File vocabspeaker\_sync_repo.ps1 -Sync
    ... -DocsRoot "D:\some\other\docs"

  Exit code: 0 = every file is identical after this run, 1 = differences remain
  (or a source is missing / empty). A gate can rely on the exit code.
#>
param(
    [switch]$Sync,
    [string]$DocsRoot = 'C:\Users\Pengc\CodeBuddy\20261012091910'
)

$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }
$dev = Split-Path -Parent $repo

function Fail([string]$msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $dev 'vocab-tool\serve.py'))) {
    Fail "dev copy not found. This script must sit in <dev>\vocabspeaker\, and <dev>\vocab-tool\serve.py must exist."
}

# 1. code: DEV -> REPO (identical relative paths)
$ToRepo = @(
    'AGENTS.md',
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

# 2. public front page: REPO -> DEV
$ToDev = @(
    'README.md'
)

function Get-Sha([string]$p) {
    if (-not (Test-Path $p -PathType Leaf)) { return 'MISSING' }
    return (Get-FileHash -Algorithm SHA256 -Path $p).Hash.Substring(0, 12)
}

# Returns ok | synced | diff | missing | empty
function Sync-One([string]$src, [string]$dst, [string]$label) {
    $hs = Get-Sha $src
    if ($hs -eq 'MISSING') {
        Write-Host ("MISSING  {0}  ({1})" -f $dst, $label) -ForegroundColor Red
        return 'missing'
    }
    # Never let an empty source overwrite a real file: a 0-byte start_pet.bat
    # sat in the docs folder for ten days and looked like a working launcher.
    if ((Get-Item $src).Length -eq 0) {
        Write-Host ("EMPTY    {0}  (source is 0 bytes, not copied)" -f $dst) -ForegroundColor Red
        return 'empty'
    }
    $hd = Get-Sha $dst
    if ($hs -eq $hd) { return 'ok' }

    if (-not $Sync) {
        Write-Host ("DIFF     {0}  {1} != {2}  ({3})" -f $dst, $hd, $hs, $label) -ForegroundColor Yellow
        return 'diff'
    }

    $dstDir = Split-Path -Parent $dst
    if ($dstDir -and -not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Copy-Item -Path $src -Destination $dst -Force
    $h2 = Get-Sha $dst
    if ($h2 -eq $hs) {
        Write-Host ("SYNC     {0}  {1} -> {2}  ({3})" -f $dst, $hd, $h2, $label) -ForegroundColor Cyan
        return 'synced'
    }
    Write-Host ("FAILED   {0}  copy did not verify ({1} != {2})" -f $dst, $h2, $hs) -ForegroundColor Red
    return 'diff'
}

$count = @{ ok = 0; synced = 0; diff = 0; missing = 0; empty = 0; skip = 0 }

Write-Host "dev  : $dev"
Write-Host "repo : $repo"
Write-Host "docs : $DocsRoot"
Write-Host "mode : $(if ($Sync) { 'SYNC (copy then verify)' } else { 'CHECK (read only)' })"
Write-Host ""

foreach ($rel in $ToRepo) {
    $st = Sync-One (Join-Path $dev $rel) (Join-Path $repo $rel) 'dev -> repo'
    $count[$st]++
}
foreach ($rel in $ToDev) {
    $st = Sync-One (Join-Path $repo $rel) (Join-Path $dev $rel) 'repo -> dev'
    $count[$st]++
}

# 3. docs: DOCS -> REPO\docs   (and .learnings\*.md -> REPO\.learnings\)
if (-not (Test-Path $DocsRoot)) {
    Write-Host ("SKIP docs  (docs root not found: {0})" -f $DocsRoot) -ForegroundColor Yellow
    $count['skip']++
}
else {
    $docPairs = @()
    Get-ChildItem $DocsRoot -File -Filter '*.md' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'AGENTS.md' } |
        ForEach-Object { $docPairs += , @($_.FullName, (Join-Path 'docs' $_.Name)) }
    $learn = Join-Path $DocsRoot '.learnings'
    if (Test-Path $learn) {
        Get-ChildItem $learn -File -Filter '*.md' -ErrorAction SilentlyContinue |
            ForEach-Object { $docPairs += , @($_.FullName, (Join-Path '.learnings' $_.Name)) }
    }
    if ($docPairs.Count -eq 0) {
        Write-Host "SKIP docs  (no *.md found under the docs root)" -ForegroundColor Yellow
        $count['skip']++
    }
    foreach ($pair in $docPairs) {
        $st = Sync-One $pair[0] (Join-Path $repo $pair[1]) 'docs -> repo'
        $count[$st]++
    }

    # AGENTS.md has a third copy in the IDE workspace. Keep all three identical from
    # the DEV copy, so "which AGENTS.md am I reading" never becomes a question.
    $st = Sync-One (Join-Path $dev 'AGENTS.md') (Join-Path $DocsRoot 'AGENTS.md') 'dev -> workspace'
    $count[$st]++
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
Write-Host ("identical {0}   synced {1}   diff {2}   missing {3}   empty-skip {4}   skipped-groups {5}" -f `
        $count['ok'], $count['synced'], $count['diff'], $count['missing'], $count['empty'], $count['skip'])
if ($count['diff'] -gt 0 -or $count['missing'] -gt 0 -or $count['empty'] -gt 0) {
    if (-not $Sync) { Write-Host "run with -Sync to copy the source side over." -ForegroundColor Yellow }
    exit 1
}
if ($count['synced'] -gt 0) {
    Write-Host "now run: git -C $repo status --short   (then commit the updated copies)" -ForegroundColor Yellow
}
Write-Host "all files identical" -ForegroundColor Green
exit 0
