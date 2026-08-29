@echo off
setlocal

rem One-click first-run setup. dependency-installer.exe starts the bundled
rem PowerShell wrapper with a process-scoped ExecutionPolicy bypass; no
rem machine or user execution-policy setting is changed.
pushd "%~dp0"
dependency-installer.exe %*
set "EXITCODE=%ERRORLEVEL%"
popd

if not "%EXITCODE%"=="0" (
  echo.
  echo EditPath dependency setup failed with exit code %EXITCODE%.
  echo See "%LOCALAPPDATA%\EditPath\dependency-install.log" for details.
  pause
)
exit /b %EXITCODE%
