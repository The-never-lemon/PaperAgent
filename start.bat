@REM Starts the app on Windows by calling scripts\launch.py.
@echo off
cd /d "%~dp0."

rem Drop Python settings inherited from the user's own shell.
rem Anaconda often leaves these set, and a leftover PYTHONHOME
rem makes the Python that uv prepares behave in confusing ways.
set "PYTHONHOME="
set "PYTHONPATH="
set "VIRTUAL_ENV="

rem Prefer uv.exe already sitting in this project, so nothing else
rem has to be installed. Do not run "where" on a quoted full path:
rem it treats the quotes as part of the filename and never matches.
rem "where" is only for the bare command name.
if exist "%~dp0tools\uv.exe" goto have_bundled

where uv >nul 2>nul
if not errorlevel 1 goto have_path

rem No project copy and no uv command. Download the 64-bit Windows build.
rem A 32-bit command prompt on 64-bit Windows reports x86 in the first
rem variable; the real system type is in the second one.
set "UV_ARCH=%PROCESSOR_ARCHITECTURE%"
if /I "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "UV_ARCH=AMD64"
if /I not "%UV_ARCH%"=="AMD64" goto unsupported_arch

echo.
echo   uv was not found. Downloading tools\uv.exe ...
echo   Do not close this window.
echo.

rem Download the latest 64-bit zip from GitHub, keep only uv.exe,
rem and delete the temporary files. Check the exit code before the
rem next "set": that command itself resets the exit code to 0.
set "UV_DEST=%~dp0tools\uv.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $zip=Join-Path $env:TEMP 'paper-agent-uv.zip'; $dest=Join-Path $env:TEMP 'paper-agent-uv'; if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }; try { Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip -UseBasicParsing; Expand-Archive -Path $zip -DestinationPath $dest -Force; $found=Get-ChildItem -Path $dest -Filter uv.exe -Recurse | Select-Object -First 1; if (-not $found) { throw 'uv.exe not found in zip' }; $folder=Split-Path -Parent $env:UV_DEST; if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }; Copy-Item $found.FullName $env:UV_DEST -Force } finally { if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }; if (Test-Path $dest) { Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue } }"
if errorlevel 1 goto download_failed
set "UV_DEST="
if not exist "%~dp0tools\uv.exe" goto download_failed
goto have_bundled

:unsupported_arch
echo.
echo   [ERROR] Automatic uv download only supports 64-bit Windows.
echo.
echo   To install uv manually, run this in PowerShell:
echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo.
pause
exit /b 1

:download_failed
echo.
echo   [ERROR] Could not download uv. Check the network and try again.
echo.
echo   To install uv manually, run this in PowerShell:
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
    echo   Startup stopped with exit code %EXITCODE%. Read the message above for details.
    pause >nul
)
exit /b %EXITCODE%
