@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  build.bat - 一键打包：PyInstaller -> 便携 zip -> Inno 安装包
REM  用法：双击运行。产物在 dist\ 下：
REM    dist\oimimo\                    onedir 程序目录
REM    dist\oimimo-portable-win64.zip  便携版
REM    dist\oimimo-setup.exe           安装包
REM ============================================================

set "ROOT=%~dp0.."
cd /d "%ROOT%"

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 uv。请先安装 uv，再运行此构建脚本。
    pause
    exit /b 1
)

set "UV_CACHE_DIR=%ROOT%\.uv-cache"
if not exist ".runtime\tmp" mkdir ".runtime\tmp"
set "TEMP=%ROOT%\.runtime\tmp"
set "TMP=%ROOT%\.runtime\tmp"
if not exist ".venv\Scripts\python.exe" uv venv .venv
if errorlevel 1 (
    echo [错误] 创建项目虚拟环境失败
    pause
    exit /b 1
)
set "PROJECT_PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "PYINSTALLER_CONFIG_DIR=%ROOT%\.pyinstaller-cache"
uv pip install --python "%PROJECT_PYTHON%" -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [错误] 使用 uv 安装构建依赖失败
    pause
    exit /b 1
)

echo [0/3] 安装当前平台的 Chromium 运行时到项目目录...
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\.playwright-browsers"
"%PROJECT_PYTHON%" -m playwright install chromium
if errorlevel 1 (
    echo [错误] Playwright Chromium 安装失败；请检查依赖和网络后重试
    pause
    exit /b 1
)

echo [1/3] PyInstaller 打包（清空重建 dist\oimimo）...
"%PROJECT_PYTHON%" -m PyInstaller --noconfirm --clean oimimo.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    pause
    exit /b 1
)

echo.
echo [2/3] 压缩便携版 zip...
if exist "dist\oimimo-portable-win64.zip" del "dist\oimimo-portable-win64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\oimimo' -DestinationPath 'dist\oimimo-portable-win64.zip' -Force"
if errorlevel 1 (
    echo [错误] zip 压缩失败
    pause
    exit /b 1
)

echo.
echo [3/3] 编译 Inno Setup 安装包...
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [错误] 未找到 Inno Setup 6，请先安装：winget install JRSoftware.InnoSetup
    pause
    exit /b 1
)
"%ISCC%" /Q packaging\installer.iss
if errorlevel 1 (
    echo [错误] Inno Setup 编译失败
    pause
    exit /b 1
)

echo.
echo ============ 打包完成 ============
echo   dist\oimimo-portable-win64.zip
echo   dist\oimimo-setup.exe
echo ==================================
pause
