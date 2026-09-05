# Launches llama-server.exe for ghost_writer using settings from config.yaml.
# Usage: powershell -File scripts\start_llm_server.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

python -m ghostwriter.llm_server_launcher @args
