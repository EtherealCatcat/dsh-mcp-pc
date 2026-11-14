@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dshpc.ps1" %*
exit /b %ERRORLEVEL%
