$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LocalRoot = Join-Path $ProjectRoot ".local"

$directories = @(
    $LocalRoot,
    (Join-Path $LocalRoot "python"),
    (Join-Path $LocalRoot "uv-cache"),
    (Join-Path $LocalRoot "temp"),
    (Join-Path $LocalRoot "cache"),
    (Join-Path $LocalRoot "config"),
    (Join-Path $LocalRoot "logs"),
    (Join-Path $LocalRoot "models"),
    (Join-Path $LocalRoot "gpu-runtime"),
    (Join-Path $LocalRoot "pycache")
)
foreach ($directory in $directories) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$env:UV_CACHE_DIR = Join-Path $LocalRoot "uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $LocalRoot "python"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $ProjectRoot ".venv"
$env:TEMP = Join-Path $LocalRoot "temp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:HF_HOME = Join-Path $LocalRoot "cache\huggingface"
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME "hub"
$env:TORCH_HOME = Join-Path $LocalRoot "cache\torch"
$env:XDG_CACHE_HOME = Join-Path $LocalRoot "cache\xdg"
$env:PYTHONPYCACHEPREFIX = Join-Path $LocalRoot "pycache"
$env:PIP_CACHE_DIR = Join-Path $LocalRoot "cache\pip"
$env:VST_DEV_ROOT = $ProjectRoot
$env:VST_DATA_ROOT = $LocalRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
