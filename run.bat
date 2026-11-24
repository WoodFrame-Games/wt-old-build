@echo off
setlocal
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  if exist "C:\msys64\ucrt64\bin\python.exe" set "PY=C:\msys64\ucrt64\bin\python.exe"
)
if not defined PY (
  echo Python not found. Install Python 3.9+ with tkinter, or edit this file.
  pause
  exit /b 1
)
%PY% "%~dp0wt_old_build.py" %*
if errorlevel 1 pause
endlocal
