@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Stop-Boujoy.ps1" -Root "%~dp0"
if errorlevel 1 pause
