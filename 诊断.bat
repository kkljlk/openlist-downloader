@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo  OpenList Downloader - startup check
echo ==========================================
echo Folder: %CD%
echo.

set "APP="
for %%F in ("OpenList*.exe") do if not defined APP set "APP=%%~fF"
if not defined APP (
  echo [X] OpenList*.exe not found.
  echo     Extract the whole ZIP first; do not run it inside the ZIP.
  goto end
)
echo [OK] App: %APP%
if exist "YunX\YunX-Desktop.exe" (
  echo [OK] YunX folder found.
) else (
  echo [!] YunX folder missing: app can start, but netdisk links cannot be parsed.
)

echo.
echo Starting app, waiting 6 seconds...
start "" "%APP%"
timeout /t 6 /nobreak >nul

tasklist | find /I "OpenList" >nul
if errorlevel 1 (
  echo [X] Process did not start. Most likely blocked by SmartScreen or antivirus.
) else (
  echo [OK] Process is running. If no window is visible, check the taskbar.
)

echo.
if exist "data\app.log" (
  echo LOG DATA:
  powershell -NoProfile -Command "Get-Content -LiteralPath 'data\app.log' -Tail 30"
) else (
  echo [!] data\app.log not found: app did not reach Python.
  echo     Unblock the ZIP, extract it fully, and add the EXE to antivirus exclusions.
)
if exist "%LOCALAPPDATA%\OpenListDownloader\app.log" (
  echo.
  echo LOG LOCALAPPDATA:
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOCALAPPDATA%\OpenListDownloader\app.log' -Tail 30"
)

:end
echo.
echo Send a screenshot of this window if it still does not start.
echo This window will close in 20 seconds.
timeout /t 20 >nul