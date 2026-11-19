@echo off
setlocal
cd /d "%~dp0"
set "TEMP=%~dp0runtime-temp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>nul
if not exist "%TEMP%" (
  echo Cannot create: %TEMP%
  echo Move the whole folder to D:\OpenList first, then run this again.
  pause
  exit /b 1
)

set "APP="
for %%F in ("OpenList*.exe") do if not defined APP set "APP=%%~fF"
if not defined APP (
  echo OpenList*.exe not found in this folder.
  pause
  exit /b 1
)

echo TEMP=%TEMP%
echo Starting: %APP%
start "" "%APP%"
exit /b 0