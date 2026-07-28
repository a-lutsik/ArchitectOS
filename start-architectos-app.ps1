$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python .\run_architectos.py --app-window @args
