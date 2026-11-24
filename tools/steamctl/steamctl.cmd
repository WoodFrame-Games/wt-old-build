@echo off
setlocal
set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
  echo Local Python for steamctl not found: %PY%
  exit /b 1
)
"%PY%" -W ignore::SyntaxWarning -m steamctl %*
exit /b %ERRORLEVEL%
