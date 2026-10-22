# Starts the local static site and its OP.GG/chat proxy on loopback only.
$ErrorActionPreference = 'Stop'
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Use PYTHON when set; otherwise detect a working Python launcher/interpreter.
$candidate = $null
if ($env:PYTHON -and (Test-Path -LiteralPath $env:PYTHON)) {
  $candidate = [pscustomobject]@{ File = $env:PYTHON; Prefix = @() }
} else {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    & $py.Source -3 -c 'import sys' *> $null
    if ($LASTEXITCODE -eq 0) { $candidate = [pscustomobject]@{ File = $py.Source; Prefix = @('-3') } }
  }
  if (-not $candidate) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
      & $python.Source -c 'import sys' *> $null
      if ($LASTEXITCODE -eq 0) { $candidate = [pscustomobject]@{ File = $python.Source; Prefix = @() } }
    }
  }
}
if (-not $candidate) {
  throw 'Python 3 is required. Install it from python.org, then re-run this script (or set the PYTHON environment variable to its python.exe path).'
}

function Test-PortFree([int]$Port) {
  return -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

foreach ($port in 8090) {
  if (-not (Test-PortFree $port)) {
    throw "Port $port is already in use. Stop the process using it, then run this script again."
  }
}

$env:PORT = '8090'
$env:BIND_HOST = '127.0.0.1'
$server = Start-Process -FilePath $candidate.File -WorkingDirectory $appDir -WindowStyle Hidden -PassThru `
  -ArgumentList @($candidate.Prefix + @('opgg_proxy.py'))

try {
  $health = $null
  for ($attempt = 0; $attempt -lt 10 -and -not $health; $attempt++) {
    try { $health = Invoke-RestMethod 'http://127.0.0.1:8091/health' } catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $health.ok) { throw 'Health check did not return ok.' }
  Write-Host "Ready: http://127.0.0.1:8090 (server PID $($server.Id))"
} catch {
  Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
  throw
}
