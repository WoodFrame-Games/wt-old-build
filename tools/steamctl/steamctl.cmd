@echo off
setlocal
set "VENDOR=%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  if exist "C:\msys64\ucrt64\bin\python.exe" set "PY=C:\msys64\ucrt64\bin\python.exe"
)
if not defined PY (
  echo Local steamctl: Python not found.
  exit /b 1
)
set "PYTHONPATH=%VENDOR%;%PYTHONPATH%"
"%PY%" -W ignore::SyntaxWarning -m steamctl %*
exit /b %ERRORLEVEL%
