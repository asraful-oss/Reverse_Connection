@echo off
rem FileShare launcher for Windows: nothing to install.
rem It builds file-share.cs with the C# compiler that ships inside Windows,
rem then runs it. If the .NET 10 SDK is installed it uses "dotnet run" instead.
setlocal
cd /d "%~dp0"
title FileShare

set /a DNV=0
where dotnet >nul 2>nul && for /f "tokens=1 delims=." %%v in ('dotnet --version 2^>nul') do set /a DNV=%%v >nul 2>nul
if %DNV% GEQ 10 (
    dotnet run file-share.cs -- %*
    goto :end
)

set "CSC="
if exist "%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not defined CSC (
    echo [!] The C# compiler that comes with Windows was not found.
    echo     Turn on ".NET Framework 4.8" in "Turn Windows features on or off",
    echo     or install the .NET SDK, then run this again.
    pause
    goto :end
)

echo Preparing FileShare...
"%CSC%" /nologo /optimize+ /warn:0 /out:"%~dp0file-share.exe" "%~dp0file-share.cs" >"%TEMP%\file-share-build.txt" 2>&1
if errorlevel 1 (
    if exist "%~dp0file-share.exe" (
        echo [*] Could not rebuild ^(is FileShare already open?^). Starting the existing copy.
    ) else (
        type "%TEMP%\file-share-build.txt"
        echo [!] Building file-share.cs failed. See the messages above.
        pause
        goto :end
    )
)
"%~dp0file-share.exe" %*

:end
endlocal
