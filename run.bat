@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%tools\steamctl\python\pythonw.exe"

if exist "%PY%" (
  set "TCL_LIBRARY=%ROOT%tools\steamctl\python\tcl\tcl8.6"
  set "TK_LIBRARY=%ROOT%tools\steamctl\python\tcl\tk8.6"
  set "PYTHONPATH=%ROOT%tools\steamctl\lib"
  "%PY%" "%ROOT%wt_old_build.py" %*
  exit /b %ERRORLEVEL%
)

rem --- fallback: системный Python, если встроенный отсутствует ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo Python not found. Re-extract the full archive.
  pause
  exit /b 1
)
%PY% "%ROOT%wt_old_build.py" %*
if errorlevel 1 pause
endlocal
