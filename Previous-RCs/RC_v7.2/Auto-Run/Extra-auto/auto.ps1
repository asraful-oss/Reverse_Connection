# ============================================================================
# auto.ps1 — one-command bootstrap for auto.js on Windows 10/11.
#
#   powershell -ExecutionPolicy Bypass -File auto.ps1 myprogram.py --hidden
#   (or just run auto.cmd, which calls this)
#
# A .js file cannot start without a JavaScript engine on the machine, so this
# starter guarantees one: it uses the system Node if present, otherwise it
# quietly fetches an official portable Node into %USERPROFILE%\.configs\runtime
# — in your profile, with NO Administrator and no prompts — then runs auto.js,
# which makes every supported language (.py .js .ts .cs .sh) "just run".
# ============================================================================
$ErrorActionPreference = 'Stop'

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$autoJs = Join-Path $dir 'auto.js'
if (-not (Test-Path $autoJs)) { Write-Error "auto.js not found next to auto.ps1 ($autoJs)"; exit 1 }

function Find-Node {
  $c = Get-Command node -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $rt = Join-Path $env:USERPROFILE '.configs\runtime\node\node.exe'
  if (Test-Path $rt) { return $rt }
  return $null
}

$node = Find-Node
if (-not $node) {
  Write-Host "[*] Setting up a private Node runtime (no admin needed)..."
  try {
    $idx = Invoke-RestMethod -UseBasicParsing -Uri 'https://nodejs.org/dist/index.json'
    $ver = ($idx | Where-Object { $_.lts } | Select-Object -First 1).version
  } catch { $ver = $null }
  if (-not $ver) { $ver = 'v22.20.0' }

  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arch = 'arm64' }
  elseif ([Environment]::Is64BitOperatingSystem) { $arch = 'x64' }
  else { $arch = 'x86' }

  $file = "node-$ver-win-$arch.zip"
  $url  = "https://nodejs.org/dist/$ver/$file"
  $rtRoot = Join-Path $env:USERPROFILE '.configs\runtime'
  New-Item -ItemType Directory -Force -Path $rtRoot | Out-Null
  $tmp = Join-Path $env:TEMP $file

  try { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp }
  catch { [System.Net.WebClient]::new().DownloadFile($url, $tmp) }

  $extract = Join-Path $rtRoot '_nodetmp'
  if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
  Expand-Archive -Force -LiteralPath $tmp -DestinationPath $extract
  $inner = Get-ChildItem -Directory $extract | Select-Object -First 1
  $target = Join-Path $rtRoot 'node'
  if (Test-Path $target) { Remove-Item -Recurse -Force $target }
  Move-Item $inner.FullName $target
  Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
  Remove-Item -Force $tmp -ErrorAction SilentlyContinue
  $node = Join-Path $target 'node.exe'
}

& $node $autoJs @args
exit $LASTEXITCODE
