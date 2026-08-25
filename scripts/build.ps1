. (Join-Path $PSScriptRoot "env.ps1")
$ErrorActionPreference = "Stop"
uv sync --extra dev --no-editable
if ($LASTEXITCODE -ne 0) { throw "uv sync 失败，退出码：$LASTEXITCODE" }
$runningPortable = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -like "$ProjectRoot\dist\voice-subtitle-translator\*"
}
if ($runningPortable) {
    throw "便携程序仍在运行，请关闭后再构建：$($runningPortable.ProcessId -join ', ')"
}
uv run --no-sync pyinstaller --noconfirm voice-subtitle-translator.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码：$LASTEXITCODE" }
$portableRoot = Join-Path $ProjectRoot "dist\voice-subtitle-translator"
$subtitleSource = [string][char]0x539F + [char]0x6587
$subtitleTranslation = [string][char]0x8BD1 + [char]0x6587
$subtitleBilingual = [string][char]0x53CC + [char]0x8BED
$subtitleChinese = [string][char]0x4E2D + [char]0x6587
foreach ($directory in @(
    "config", "models", "cache", "logs", "temp", "gpu-runtime", "state",
    "subtitles\$subtitleSource", "subtitles\$subtitleChinese",
    "subtitles\$subtitleTranslation", "subtitles\$subtitleBilingual"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $portableRoot "data\$directory") | Out-Null
}
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination $portableRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $portableRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") -Destination $portableRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "vendor\manifest.json") -Destination (Join-Path $portableRoot "VENDOR_MANIFEST.json") -Force
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "artifacts") | Out-Null
$version = (uv run --no-sync python -c "from voice_subtitle_translator import __version__; print(__version__)")
if ($LASTEXITCODE -ne 0) { throw "读取版本号失败，退出码：$LASTEXITCODE" }
$zip = Join-Path $ProjectRoot "artifacts\voice-subtitle-translator-$version-windows-x64.zip"
$archiveCreated = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        Compress-Archive -Path (Join-Path $ProjectRoot "dist\voice-subtitle-translator\*") -DestinationPath $zip -Force
        $archiveCreated = $true
        break
    }
    catch {
        if ($attempt -eq 5) { throw }
        Write-Warning "ZIP is temporarily locked; retrying in 2 seconds ($attempt/5)."
        Start-Sleep -Seconds 2
    }
}
if (-not $archiveCreated) { throw "Failed to create the portable ZIP." }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zip.sha256" -Value "$hash  $(Split-Path -Leaf $zip)" -Encoding ascii
Write-Output $zip
Write-Output $hash
