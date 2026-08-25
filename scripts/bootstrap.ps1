. (Join-Path $PSScriptRoot "env.ps1")
uv python install 3.11
uv sync --extra dev --no-editable
