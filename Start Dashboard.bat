@echo off
REM Double-click this file to open the stock planner dashboard.
REM The first time, it sets itself up (needs internet, takes a minute or two).
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed yet.
  echo Please install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" on the first screen. Then double-click this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. Please wait...
  py -m venv .venv
  if errorlevel 1 goto fail
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 goto fail
)
echo.
echo Opening the dashboard in your web browser.
echo Leave this window open while you use it. Close it to stop.
echo.
".venv\Scripts\python.exe" dashboard.py
pause
exit /b 0
:fail
echo.
echo Setup did not finish. Check your internet connection and try again.
rmdir /s /q .venv >nul 2>nul
pause
exit /b 1
