@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "APP=%~dp0OpenList下载器.exe"

if exist "%APP%" (
  start "" "%APP%"
  exit /b 0
)

if exist "%~dp0dist\OpenList下载器.exe" (
  start "" "%~dp0dist\OpenList下载器.exe"
  exit /b 0
)

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0app.py"
  exit /b 0
)

where python.exe >nul 2>nul
if %errorlevel%==0 (
  python.exe "%~dp0app.py"
  exit /b 0
)

echo.
echo 没有找到 OpenList下载器.exe，也没有找到 Python。
echo 请直接双击 OpenList下载器.exe，或安装 Python 3.10+ 后运行 app.py。
echo.
pause
exit /b 1