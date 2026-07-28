@echo off
setlocal
cd /d "%~dp0"
python run_architectos.py --app-window %*
