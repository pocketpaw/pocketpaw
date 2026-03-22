@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".git" (
  echo Run this from pocketpaw repo root. Current: %CD%
  exit /b 1
)
git apply --whitespace=fix "lmstudio-integration\pocketpaw-lmstudio-full.patch"
if errorlevel 1 (
  echo If apply failed: repo may already contain changes, or use: git apply --reject
  exit /b 1
)
echo Patch applied.
endlocal
