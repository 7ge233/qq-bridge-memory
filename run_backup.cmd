@echo off
rem Daily backup (design doc 6.6): keep the newest 7 copies.
rem This script does not install a scheduled task -- create one yourself
rem in Windows Task Scheduler if you want it to run automatically.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" backup.py --keep 7
