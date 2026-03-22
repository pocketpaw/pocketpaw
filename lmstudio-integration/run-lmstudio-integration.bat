@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "HOST=http://127.0.0.1:1234"
set "MODEL="

if not "%~1"=="" set "HOST=%~1"
if not "%~2"=="" set "MODEL=%~2"

where py >nul 2>&1
if %errorlevel% equ 0 (
  py -3 apply_lmstudio_config.py --host "!HOST!" --model "!MODEL!"
  goto :end
)

where python >nul 2>&1
if %errorlevel% equ 0 (
  python apply_lmstudio_config.py --host "!HOST!" --model "!MODEL!"
  goto :end
)

where python3 >nul 2>&1
if %errorlevel% equ 0 (
  python3 apply_lmstudio_config.py --host "!HOST!" --model "!MODEL!"
  goto :end
)

echo Python not found. Install Python 3 or add py launcher to PATH.
exit /b 1

:end
endlocal
