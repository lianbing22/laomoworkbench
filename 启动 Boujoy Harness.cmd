@echo off
setlocal
set "ROOT=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\Start-Boujoy.ps1" -Root "%ROOT%" -Check
if errorlevel 1 (
  echo.
  echo Boujoy Harness did not start. Read the error above, then install the Windows runtime described in README-Windows.md.
  pause
  exit /b 1
)

start "Boujoy Harness" /min powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0windows\Start-Boujoy.ps1" -Root "%ROOT%"
exit /b 0
