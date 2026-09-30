@REM 文件作用：供 Windows 用户双击启动项目，并调用一键启动脚本。
@echo off
rem 先切到 UTF-8，后面的中文提示才能正常显示。
chcp 65001 >nul
cd /d "%~dp0."

rem 清掉从用户自己的命令行带进来的 Python 相关变量。
rem 装了 Anaconda 的电脑常常设着这些变量。残留的 PYTHONHOME 会让后面准备的 Python 行为变得很难懂。
set "PYTHONHOME="
set "PYTHONPATH="
set "VIRTUAL_ENV="

rem 优先用项目目录里已经放好的 uv.exe，这样别人什么都不用装。
rem 不要对带引号的绝对路径使用 where：它会把引号当成文件名的一部分，结果永远找不到。
rem 只有「系统命令里有没有 uv」这一步才用 where。
if exist "%~dp0tools\uv.exe" goto have_bundled

where uv >nul 2>nul
if not errorlevel 1 goto have_path

rem 项目里没有 uv.exe，系统里也没有 uv。只自动下载 64 位 Windows 版。
rem 32 位命令行跑在 64 位系统上时，第一项会显示成 x86，真正的系统位数在第二项里。
set "UV_ARCH=%PROCESSOR_ARCHITECTURE%"
if /I "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "UV_ARCH=AMD64"
if /I not "%UV_ARCH%"=="AMD64" goto unsupported_arch

echo.
echo   本机没有 uv，正在下载到 tools\uv.exe ...
echo   下载期间请不要关闭这个窗口。
echo.

rem 下载 GitHub 上的最新 64 位压缩包，解压后只留下 uv.exe，临时文件用完就删。
rem 先记下退出码再清掉临时变量。set 命令自己会把退出码改成 0，不能先清再判断。
set "UV_DEST=%~dp0tools\uv.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $zip=Join-Path $env:TEMP 'paper-agent-uv.zip'; $dest=Join-Path $env:TEMP 'paper-agent-uv'; if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }; try { Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip -UseBasicParsing; Expand-Archive -Path $zip -DestinationPath $dest -Force; $found=Get-ChildItem -Path $dest -Filter uv.exe -Recurse | Select-Object -First 1; if (-not $found) { throw 'uv.exe not found in zip' }; $folder=Split-Path -Parent $env:UV_DEST; if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }; Copy-Item $found.FullName $env:UV_DEST -Force } finally { if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }; if (Test-Path $dest) { Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue } }"
if errorlevel 1 goto download_failed
set "UV_DEST="
if not exist "%~dp0tools\uv.exe" goto download_failed
goto have_bundled

:unsupported_arch
echo.
echo   [ERROR] 自动下载 uv 目前只支持 64 位 Windows。
echo.
echo   可以在 PowerShell 里手动安装：
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
pause
exit /b 1

:download_failed
echo.
echo   [ERROR] 下载 uv 失败。请检查网络后重试。
echo.
echo   也可以在 PowerShell 里手动安装：
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
pause
exit /b 1

:have_path
set "UV=uv"
goto launch

:have_bundled
set "UV=%~dp0tools\uv.exe"
goto launch

:launch
"%UV%" run python "%~dp0scripts\launch.py" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo   启动没有完成，退出码 %EXITCODE%。请看上面的说明。
    pause >nul
)
exit /b %EXITCODE%
