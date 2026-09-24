@echo off
title DL-SplatGenerator - server (close this window to stop)
cd /d "%~dp0"

rem This file sits at the project root; the app itself is in project\.
rem launcher.py resolves its own folder, so it does not care about the
rem working directory -- only the path below has to be right.

rem The venv (Python 3.12) carries FastAPI and torch, so it starts the viewer
rem AND the capture backend. Plain python starts the viewer alone, which is
rem still fully usable -- the "Make a scene" panel simply stays hidden.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "project\launcher.py" %*
  goto :done
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on your PATH.
  echo Install Python 3 from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
echo No .venv found - starting the viewer without the capture backend.
python "project\launcher.py" %*

:done
pause
