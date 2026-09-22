# Launch nbus-term on Windows, bootstrapping everything it needs.
# If `uv` isn't installed it's fetched automatically; uv then provides the
# Python interpreter and the dependencies from pyproject.toml.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Have-Uv { $null -ne (Get-Command uv -ErrorAction SilentlyContinue) }

if (-not (Have-Uv)) {
    $local = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path (Join-Path $local "uv.exe")) { $env:Path = "$local;$env:Path" }
}

if (-not (Have-Uv)) {
    Write-Host "uv not found - installing it (one-time)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $local = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path (Join-Path $local "uv.exe")) { $env:Path = "$local;$env:Path" }
}

& uv run --quiet nbus-term @args
