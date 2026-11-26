param([string]$In)
# Step-by-step timing of every Word COM primitive used by _word_pages.ps1.
# Run this before "optimising" the pagination scan again -- it tells you which
# primitive is actually expensive on THIS machine + Office build.
#
# Measured 2026-11-18, Word 16.0, 39 pages / 2191 paragraphs / 2182 entries:
#   open 1.0-4.0s | ComputeStatistics 4-5s | Paragraphs.Count 0.14s
#   Content.Text 0.20s | GoTo(page) 0.25s | Range.Text 0.01s
#   Range.Information(3) 0.21-0.28s  <-- per paragraph = the 9-minute path
#   Repaginate() 0.02s (pagination already cached; changing view does not help)
#
# Note the ORDER effect: doing 39 GoTo calls before reading Content.Text
# measured 43.8s (1.1s each) instead of ~10s, because each call re-triggered
# pagination. _word_pages.ps1 therefore reads Content.Text first.
#
# ASCII only (PowerShell 5.1 reads a BOM-less .ps1 as GBK).
$ErrorActionPreference = 'Stop'

function T($label, $sw) { "{0,-26} = {1,7:N2}s" -f $label, $sw.Elapsed.TotalSeconds }

$word = $null
$doc = $null
try {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  T "new Word.Application" $sw

  $sw.Restart()
  $doc = $word.Documents.Open($In, $false, $true)
  T "Documents.Open(RO)" $sw

  $sw.Restart()
  $pages = [int]$doc.ComputeStatistics(2)
  T "ComputeStatistics(pages)" $sw
  "  -> pages = $pages"

  $sw.Restart()
  $p2 = [int]$doc.ComputeStatistics(2)
  T "ComputeStatistics again" $sw

  $sw.Restart()
  $np = [int]$doc.Paragraphs.Count
  T "Paragraphs.Count" $sw
  "  -> paras = $np"

  $sw.Restart()
  $all = [string]$doc.Content.Text
  T "Content.Text" $sw
  "  -> len = $($all.Length)"

  $sw.Restart()
  $chunks = $all -split "`r"
  T "split Content.Text on CR" $sw
  "  -> chunks = $($chunks.Count)"
  "  -> last char code = $([int][char]$all[$all.Length-1])"
  "  -> CR count = $(($all.ToCharArray() | Where-Object { $_ -eq "`r" }).Count)"

  $sw.Restart()
  $starts = New-Object System.Collections.ArrayList
  for ($p = 1; $p -le $pages; $p++) {
    $r = $doc.GoTo(1, 1, $p)
    [void]$starts.Add([int]$r.Start)
  }
  T ("GoTo page start x{0}" -f $pages) $sw
  "  -> first 5: " + (($starts | Select-Object -First 5) -join ',')

  # One representative page: how expensive is per-page work?
  $sw.Restart()
  $a = [int]$starts[0]; $b = [int]$starts[1]
  $rng = $doc.Range($a, $b)
  T "Range(page1)" $sw

  $sw.Restart()
  $n1 = [int]$rng.Paragraphs.Count
  T "page1 Paragraphs.Count" $sw
  "  -> n = $n1"

  $sw.Restart()
  $t1 = [string]$rng.Text
  T "page1 Range.Text" $sw
  $k1 = @($t1 -split "`r")
  "  -> len = $($t1.Length), split chunks = $($k1.Count)"
  "  -> chunk[last] empty? = $($k1[$k1.Count-1] -eq '')"
  "  -> tail = [" + $t1.Substring([Math]::Max(0, $t1.Length - 40)).Replace("`r", "<CR>") + "]"

  # Cost of ONE per-paragraph Information(3) round-trip (the slow path).
  # Clamped: a 7-paragraph sample document would otherwise throw on Item(8).
  $sw.Restart()
  $probe = [Math]::Min(50, $np)
  for ($i = 1; $i -le $probe; $i++) {
    $pr = $doc.Paragraphs.Item($i).Range
    [void]([int]$pr.Information(3))
  }
  T ("Information(3) x{0}" -f $probe) $sw
  $per = $sw.Elapsed.TotalSeconds / $probe
  "  -> {0:N1} ms/para => full doc estimate {1:N0}s for {2} paras" -f ($per * 1000), ($per * $np), $np

  # Page boundary sanity: what sits at each page start?
  $sw.Restart()
  $samples = @()
  for ($p = 2; $p -le [Math]::Min($pages, 6); $p++) {
    $s = [int]$starts[$p - 1]
    $prev = [string]$doc.Range($s - 1, $s).Text
    $samples += ("p{0}@{1} prevChar={2}" -f $p, $s, [int][char]$prev[0])
  }
  T "page-start prev char x5" $sw
  foreach ($s in $samples) { "  -> $s" }
}
catch {
  "ERROR at: " + $_.Exception.Message
}
finally {
  if ($doc) { try { $doc.Close(0) } catch { } }
  if ($word) {
    try { $word.Quit() } catch { }
    try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) } catch { }
  }
  [GC]::Collect()
}
