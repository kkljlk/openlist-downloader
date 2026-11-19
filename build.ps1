param(
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += @(
        "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            try {
                $version = & $candidate -c "import sys; print(sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and $version) { return $candidate }
            } catch { }
        }
    }
    throw "没有找到可用的 Python 3.10+。"
}

$Python = Find-Python
Write-Host "使用 Python: $Python"

& $Python -m pip install --disable-pip-version-check -q pyinstaller
if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败" }

$consoleFlag = if ($Console) { "--console" } else { "--noconsole" }
$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    $consoleFlag,
    "--name", "OpenList下载器",
    "--icon", "assets\icon.ico",
    "--add-data", "static;static",
    "--add-data", "tools\aria2;tools\aria2",
    "--collect-all", "webview",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "webview.platforms.edgechromium",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc",
    "desktop.py"
)

& $Python @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$distDir = Join-Path $Root "dist"
Copy-Item "README.md" $distDir -Force
Copy-Item "LICENSE" $distDir -Force
Copy-Item "使用说明.txt" $distDir -Force
Copy-Item "THIRD-PARTY-NOTICES.txt" $distDir -Force
Copy-Item "启动.bat" $distDir -Force
Copy-Item "诊断.bat" $distDir -Force
Copy-Item "启动-修复临时目录.bat" $distDir -Force
Copy-Item "tools\aria2\COPYING" (Join-Path $distDir "ARIA2-COPYING.txt") -Force
if (Test-Path "tools\aria2\LICENSE.OpenSSL") {
    Copy-Item "tools\aria2\LICENSE.OpenSSL" (Join-Path $distDir "ARIA2-LICENSE-OpenSSL.txt") -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $distDir "screenshots") | Out-Null
if (Test-Path "screenshots") {
    Copy-Item "screenshots\*" (Join-Path $distDir "screenshots") -Force -Recurse
}
$yunxDist = Join-Path $distDir "YunX"
if (Test-Path "YunX") {
    Remove-Item $yunxDist -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item "YunX" $yunxDist -Recurse -Force
    if (Test-Path "YUNX-NOTICE.txt") {
        Copy-Item "YUNX-NOTICE.txt" $distDir -Force
    }
    if (Test-Path "YunX\LICENSE") {
        Copy-Item "YunX\LICENSE" (Join-Path $distDir "YUNX-LICENSE.txt") -Force
    }
} else {
    Remove-Item $yunxDist -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "构建完成：$distDir\OpenList下载器.exe"
Get-Item "$distDir\OpenList下载器.exe" | Select-Object FullName, @{Name="SizeMB"; Expression={[math]::Round($_.Length/1MB,1)}}