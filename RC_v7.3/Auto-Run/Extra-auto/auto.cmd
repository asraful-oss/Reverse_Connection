@echo off
rem ==========================================================================
rem auto.cmd — one-command bootstrap for auto.js on Windows (no admin).
rem
rem   auto.cmd myprogram.py --hidden
rem   auto.cmd myprogram.cs --hidden      rem .py .js .ts .cs .sh all work
rem
rem Delegates to auto.ps1, which finds Node (or fetches a private portable copy
rem into %USERPROFILE%\.configs\runtime, no Administrator) and runs auto.js.
rem ==========================================================================
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0auto.ps1" %*
exit /b %errorlevel%
