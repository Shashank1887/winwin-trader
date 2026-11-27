@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo [FAILED] Deploy aborted (exit code %EXITCODE%). Fix the issue above and try again.
) else (
    echo [DONE] Deploy check complete.
)
pause
exit /b %EXITCODE%
