param([int]$Port = 8899)
# ASCII only: PowerShell 5.1 reads BOM-less .ps1 as ANSI, non-ASCII breaks quoting.
# Paths are derived from this file's own location - never hard-code a machine.
$dir = $PSScriptRoot
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }

# python: local_python.txt (dev override) -> ..\runtime\python.exe (dist) -> PATH
$py = ''
$lp = Join-Path $dir 'local_python.txt'
if (Test-Path $lp) { $py = (Get-Content $lp -TotalCount 1).Trim() }
if (-not $py) {
  $rt = Join-Path (Split-Path -Parent $dir) 'runtime\python.exe'
  if (Test-Path $rt) { $py = $rt }
}
if (-not $py) { $py = 'python' }

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conn) {
  Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
  Write-Output ("killed pid " + $c.OwningProcess)
}
Start-Sleep -Seconds 1

Start-Process -FilePath $py -ArgumentList 'serve.py', "$Port" -WorkingDirectory $dir `
  -WindowStyle Hidden -RedirectStandardError (Join-Path $dir '_serve.log')
Start-Sleep -Seconds 2
Write-Output "started serve.py on $Port"
