@echo off
cd /d "%~dp0."

rem Clear interpreter-related variables inherited from the user's shell.
rem Colleagues with Anaconda installed often have these set, and a leftover
rem PYTHONHOME makes uv's managed interpreter behave in confusing ways.
set "PYTHONHOME="
set "PYTHONPATH="
set "VIRTUAL_ENV="

rem Prefer the uv bundled in this folder so nothing has to be installed.
rem Note: do not use "where" on an absolute path - it treats the quotes as part
rem of the filename and never matches. Only check PATH for the bare command.
if exist "%~dp0tools\uv.exe" (
    set "UV=%~dp0tools\uv.exe"
) else (
    set "UV=uv"
    where uv >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   [ERROR] uv was not found.
        echo.
        echo   This package should contain tools\uv.exe. If that file is missing,
        echo   the archive is incomplete - please ask for a fresh copy.
        echo.
        echo   To install uv manually, run this in PowerShell:
        echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
        echo.
        pause
        exit /b 1
    )
)

"%UV%" run python "%~dp0scripts\launch.py" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo   Startup stopped with exit code %EXITCODE%. Read the message above for details.
    pause >nul
)
exit /b %EXITCODE%
