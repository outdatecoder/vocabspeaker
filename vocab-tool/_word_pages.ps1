param([string]$In, [string]$Out, [string]$CancelFlag = "", [int]$VerifySamples = 12)
# Ask Word which page each numbered entry begins on, write [{entry,page,page_end,text}] as JSON.
#
# Measured cost of Word COM primitives on a 39-page / 2191-paragraph book
# (see _tmp_prims.ps1 / _tmp_prims2.ps1; numbers are per call, print view):
#   Documents.Open(ReadOnly)      1.0 - 4.0s   (once)
#   ComputeStatistics(pages)      4 - 5s       (once, cached afterwards)
#   Paragraphs.Count              0.14s        (once)
#   Content.Text                  0.20s        (once, whole document)
#   GoTo(wdGoToPage)              0.24s        <-- 39 pages = ~10s
#   Range.Text / Paragraphs.Count 0.01s        (per page, cheap)
#   Range.Information(3)          0.25s        <-- PER PARAGRAPH = 9 min for 2191
#   Repaginate()                  0.02s        (pagination was already cached;
#                                               switching view does NOT help)
#
# So the old design -- one Information(3) per paragraph -- was the whole
# slowdown, and the "per-page text" shortcut that was meant to avoid it bailed
# out on page 1 because it compared `text -split CR` against
# Range.Paragraphs.Count, and PowerShell `-split` drops the trailing empty
# chunk while Word still counts that paragraph. One-off-by-one => fallback to
# the 9-minute path on every run.
#
# Current design: take the whole document text ONCE, take the page-start
# character offsets with one GoTo per page (~10s total), then decide pages
# locally by character offset. This removes paragraph-splitting ambiguity
# completely (a paragraph that straddles a page break is one line here, so its
# start page and end page both come from the same offset arithmetic), and it is
# ~25x faster than the per-paragraph path.
#
# Correctness (this file used to be silently wrong for 2 entries, so trust
# nothing): the alignment between Content.Text character indices and Word's own
# Range indices is checked by fetching the same characters through BOTH channels
# and comparing them, and the computed page of a sample of entries is compared
# against Word's own Information(3). Any mismatch falls back to the slow path
# (correctness first, never silently).
#
# A word entry belongs to the page where it BEGINS (a reader sees it there).
# wdActiveEndPageNumber reports the END of a range, so entries whose last line
# spills onto the next page used to be filed under that page and dropped from
# their own page's build (entry 232 "grandpa" begins on p4, not p5).
#
# ASCII only: PowerShell 5.1 decodes a BOM-less .ps1 as GBK and non-ASCII breaks it.

$ErrorActionPreference = 'Stop'
$sw = [Diagnostics.Stopwatch]::StartNew()

$script:rc = 0
$script:canceled = $false
$script:starts = @()
$word = $null
$doc = $null
$rows = New-Object System.Collections.ArrayList

function Test-Cancel {
  if ($CancelFlag -and (Test-Path $CancelFlag)) { return $true }
  return $false
}

# Page number (1-based) that a character offset belongs to. Binary search over
# the GoTo-derived page starts; offset < starts[0] is clamped to page 1.
function Get-PageOf([int]$o) {
  $s = $script:starts
  if ($s.Count -eq 0) { return 1 }
  if ($o -le $s[0]) { return 1 }
  $lo = 0
  $hi = $s.Count - 1
  while ($lo -lt $hi) {
    $mid = [int][Math]::Floor(($lo + $hi + 1) / 2)
    if ($s[$mid] -le $o) { $lo = $mid } else { $hi = $mid - 1 }
  }
  return $lo + 1
}

$fallbackReason = ""

try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $doc = $word.Documents.Open($In, $false, $true)     # ReadOnly, no repair prompt
  Write-Output ("word version = " + $word.Version)

  $pageCount = [int]$doc.ComputeStatistics(2)         # wdStatisticPages
  $paraCount = [int]$doc.Paragraphs.Count
  if ($pageCount -lt 1) { $pageCount = 1 }
  Write-Output ("doc: paras={0} pages={1}" -f $paraCount, $pageCount)

  # ---------- whole-document text FIRST (one call) ----------
  # Order matters: reading Content.Text once pays for the full layout, after
  # which each GoTo costs ~0.25s. Doing GoTo first measured 43.8s for 39 pages
  # (1.1s each) because every call re-triggered pagination.
  $all = ""
  $allEnd = 0
  if (-not $script:canceled) {
    $all = [string]$doc.Content.Text
    $allEnd = [int]$doc.Content.End
    Write-Output ("content text = {0} chars, offsets 0..{1}" -f $all.Length, $allEnd)
    if ($all.Length -lt 1) { $fallbackReason = "empty document text" }
  }

  # ---------- page start offsets: one GoTo per page ----------
  if (-not $script:canceled -and -not $fallbackReason) {
    for ($p = 1; $p -le $pageCount; $p++) {
      if (Test-Cancel) { $script:canceled = $true; break }
      $script:starts += [int]($doc.GoTo(1, 1, $p)).Start
      # Progress for the web UI (webgen.pagescan parses "... pages N / M").
      if (($p % 10) -eq 0) { Write-Output ("... pages {0} / {1}" -f $p, $pageCount) }
    }
    Write-Output ("page starts = {0} offsets, {1:N1}s" -f $script:starts.Count, $sw.Elapsed.TotalSeconds)
  }

  if (-not $script:canceled -and -not $fallbackReason) {
    if ($script:starts.Count -lt $pageCount) {
      $fallbackReason = "page starts incomplete ({0}/{1})" -f $script:starts.Count, $pageCount
    }
    else {
      for ($i = 1; $i -lt $script:starts.Count; $i++) {
        if ($script:starts[$i] -le $script:starts[$i - 1]) {
          $fallbackReason = "page starts not strictly increasing at page {0} ({1} <= {2})" -f ($i + 1), $script:starts[$i], $script:starts[$i - 1]
          break
        }
      }
    }
  }

  # ---------- alignment check: same characters through two independent channels ----------
  if (-not $script:canceled -and -not $fallbackReason) {
    $probes = @($script:starts[0])
    if ($script:starts.Count -gt 2) { $probes += $script:starts[[int][Math]::Floor($script:starts.Count / 2)] }
    if ($script:starts.Count -gt 1) { $probes += $script:starts[$script:starts.Count - 1] }
    foreach ($s in $probes) {
      $len = [Math]::Min(16, $all.Length - $s)
      if ($s -lt 0 -or $len -le 0) { continue }
      $viaWord = [string]$doc.Range($s, $s + $len).Text
      $viaText = $all.Substring($s, $len)
      if ($viaWord -cne $viaText) {
        $fallbackReason = "text/index misalignment at offset {0} (word='{1}' text='{2}')" -f $s, ($viaWord -replace "`r", "<CR>"), ($viaText -replace "`r", "<CR>")
        break
      }
    }
    if (-not $fallbackReason) { Write-Output "alignment check = ok (3 offsets)" }
  }

  # ---------- build entries from the continuous text ----------
  if (-not $script:canceled -and -not $fallbackReason) {
    $chunks = @($all -split "`r")
    $n = 0
    $off = 0
    $cur = $null
    foreach ($c in $chunks) {
      $lineOff = $off
      $off += $c.Length + 1
      if ($lineOff -ge $allEnd) { continue }          # trailing empty chunk after the last CR
      if ($c -match '^\s*\d{1,5}\s+\S') {
        $n++
        $cur = [pscustomobject]@{
          entry  = $n
          off    = $lineOff
          endOff = $lineOff + $c.TrimEnd().Length
          text   = $c.Trim()
          page   = 0
          page_end = 0
        }
        [void]$rows.Add($cur)
      }
      elseif ($null -ne $cur -and $c.Trim().Length -gt 0) {
        # No leading number = a continuation line of the previous entry
        # (the Chinese gloss wrapped). It still belongs to the page it STARTED on.
        $cur.text = ($cur.text + " " + $c.Trim()).Trim()
        $cur.endOff = $lineOff + $c.TrimEnd().Length
      }
    }
    Write-Output ("entries = {0} (lines = {1})" -f $rows.Count, $chunks.Count)

    foreach ($r in $rows) {
      $r.page = Get-PageOf $r.off
      $e = [Math]::Max($r.off, $r.endOff - 1)
      $r.page_end = [Math]::Max($r.page, (Get-PageOf $e))
    }

    # Independent truth source: Word's own page number for that character.
    if ($VerifySamples -gt 0 -and $rows.Count -gt 0) {
      $step = [Math]::Max(1, [int][Math]::Floor($rows.Count / $VerifySamples))
      $checked = 0
      $bad = 0
      for ($i = 0; $i -lt $rows.Count -and $checked -lt $VerifySamples; $i += $step) {
        if (Test-Cancel) { $script:canceled = $true; break }
        $r = $rows[$i]
        if ($r.off + 1 -gt $allEnd) { continue }
        $wp = [int]$doc.Range($r.off, $r.off + 1).Information(3)
        $checked++
        if ($wp -ne $r.page) {
          $bad++
          Write-Output ("  VERIFY mismatch entry {0}: computed p{1}, word says p{2}" -f $r.entry, $r.page, $wp)
        }
      }
      Write-Output ("verify = {0} sampled, {1} mismatch" -f $checked, $bad)
      if ($bad -gt 0) { $fallbackReason = "page verification failed ({0}/{1} mismatch)" -f $bad, $checked }
    }
  }

  if ($script:canceled) {
    Write-Output "canceled before writing"
  }
  elseif (-not $fallbackReason -and $rows.Count -gt 0) {
    Write-Output "scan mode = offset (content text + page starts)"
  }
  else {
    if (-not $fallbackReason) { $fallbackReason = "no numbered entries in offset scan" }
    Write-Output ("fallback reason = " + $fallbackReason)
    Write-Output ("scan mode = slow (per-paragraph, ~{0:N0}s for {1} paragraphs)" -f (0.25 * $paraCount), $paraCount)

    # ---------- slow path: one COM round-trip per paragraph ----------
    $rows = New-Object System.Collections.ArrayList
    $n = 0
    for ($i = 1; $i -le $paraCount; $i++) {
      if (Test-Cancel) { $script:canceled = $true; break }
      $r = $doc.Paragraphs.Item($i).Range
      $t = [string]$r.Text
      if ($t -match '^\s*\d{1,5}\s+\S') {
        $n++
        $pEnd = [int]$r.Information(3)                 # wdActiveEndPageNumber = 3
        $rs = $r.Duplicate
        $rs.Collapse(1)                                # wdCollapseStart = 1
        $pStart = [int]$rs.Information(3)
        if ($pStart -gt $pEnd) { $pStart = $pEnd }
        [void]$rows.Add([pscustomobject]@{ entry = $n; page = $pStart; page_end = $pEnd; text = $t.Trim() })
      }
      if (($i % 250) -eq 0) { Write-Output ("... para {0} / {1}" -f $i, $paraCount) }
    }
  }

  if (-not $script:canceled) {
    Write-Output ("entries = {0}, elapsed = {1:N1}s" -f $rows.Count, $sw.Elapsed.TotalSeconds)

    if ($rows.Count -eq 0) { throw "no numbered entries found" }

    # Entries whose paragraph spans a page break: their tail line sits on the next
    # page. Reported by number only (no text -> no console encoding trouble).
    $cross = @($rows | Where-Object { $_.page -ne $_.page_end })
    Write-Output ("cross-page entries = {0}" -f $cross.Count)
    foreach ($c in $cross) {
      Write-Output ("  entry {0}: starts p{1}, ends p{2}" -f $c.entry, $c.page, $c.page_end)
    }

    # NOTE: PowerShell variable names are case-insensitive -- naming this `$out`
    # clobbered the `$Out` path parameter and made Set-Content fail.
    $finalRows = $rows | Select-Object entry, page, page_end, text
    $finalRows | ConvertTo-Json -Depth 3 -Compress | Set-Content -Path $Out -Encoding UTF8
    Write-Output ("written: {0}" -f $Out)
  }
}
catch {
  Write-Output ("ERROR: " + $_.Exception.Message)
  $script:rc = 1
}
finally {
  if ($doc) { try { $doc.Close(0) } catch { } }
  if ($word) {
    try { $word.Quit() } catch { }
    try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) } catch { }
  }
  [GC]::Collect()
}
exit $script:rc
