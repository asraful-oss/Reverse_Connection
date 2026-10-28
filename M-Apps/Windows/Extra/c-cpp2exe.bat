@echo off
rem ======================================================================
rem  C / C++ to EXE - Automated Builder  (v2, fixed)
rem
rem  Use it by double-clicking it, or by dragging a .c / .cpp file onto it.
rem
rem  Why v2: the old version could crash on certain input, and when a .bat
rem  crashes Windows closes the window instantly so you never see why.
rem  Now the real builder runs inside a child cmd, and this outer part
rem  ALWAYS waits for a key press, whatever happens inside.
rem ======================================================================

if "%~1"=="--c2e-inner" goto :inner

setlocal EnableExtensions
set "C2E_DROP=%~1"
cmd /d /c call "%~f0" --c2e-inner
set "C2E_RC=%ERRORLEVEL%"
echo.
if "%C2E_RC%"=="0" echo  ==== Builder finished. ====
if not "%C2E_RC%"=="0" echo  ==== Builder stopped - exit code %C2E_RC%. Read the messages above. ====
echo.
echo  Press any key to close this window . . .
pause >nul
exit /b %C2E_RC%


rem ======================================================================
rem  The real builder
rem ======================================================================
:inner
setlocal EnableExtensions DisableDelayedExpansion
title C / C++ to EXE - Automated Builder
color 0B
set "BATDIR=%~dp0"
set "ORIGDIR=%CD%"

echo.
echo  ==========================================
echo       C / C++ to EXE - Automated Builder
echo  ==========================================
echo.

rem ---------------------------------------------------------------------
rem  Step 1: find a folder that has BOTH gcc.exe and g++.exe
rem ---------------------------------------------------------------------
echo [1/4] Looking for GCC / G++ ...
set "CCBIN="
for %%D in ("C:\msys64\ucrt64\bin" "C:\msys64\mingw64\bin" "C:\msys2\ucrt64\bin" "D:\msys64\ucrt64\bin" "%LOCALAPPDATA%\Programs\msys64\ucrt64\bin" "%LOCALAPPDATA%\msys64\ucrt64\bin" "%USERPROFILE%\scoop\apps\msys2\current\ucrt64\bin") do call :try_bin "%%~D"
if not defined CCBIN for /f "delims=" %%P in ('where g++.exe 2^>nul') do call :try_bin "%%~dpP."
if defined CCBIN goto :compiler_found

echo [INFO] No GCC / G++ found yet. Looking for an MSYS2 installation ...
call :find_msys_root
if defined MSYSROOT goto :install_gcc

rem ---------------------------------------------------------------------
rem  Step 2: MSYS2 is missing - install it with winget
rem ---------------------------------------------------------------------
echo [2/4] MSYS2 is not installed.
where winget.exe >nul 2>nul
if errorlevel 1 goto :no_winget
echo [INFO] winget can download and install MSYS2 now. Needs internet, takes a few minutes.
echo [INFO] Windows may show a permission prompt - click Yes.
choice /c YN /n /m "Install MSYS2 with winget now? [Y/N] "
if errorlevel 2 goto :cancelled
echo.
winget install --id MSYS2.MSYS2 -e --source winget --accept-package-agreements --accept-source-agreements
set "WINGET_RC=%ERRORLEVEL%"
echo.
call :find_msys_root
if defined MSYSROOT goto :install_gcc
echo [ERROR] MSYS2 still not found after winget. winget exit code: %WINGET_RC%
echo         Install it by hand from https://www.msys2.org/ into C:\msys64 and run this again.
exit /b 1

:no_winget
echo [ERROR] winget is not available on this PC.
echo         Install MSYS2 from https://www.msys2.org/ into C:\msys64 and run this again.
exit /b 1

rem ---------------------------------------------------------------------
rem  Step 3: MSYS2 exists but GCC is missing - install the toolchain
rem ---------------------------------------------------------------------
:install_gcc
echo [OK] MSYS2 found at "%MSYSROOT%"
call :try_bin "%MSYSROOT%\ucrt64\bin"
if defined CCBIN goto :compiler_found
echo [3/4] GCC is not installed inside MSYS2 yet.
echo [INFO] Will run: pacman -S --needed base-devel mingw-w64-ucrt-x86_64-toolchain
echo [INFO] Needs internet and can take several minutes. Do not close this window.
choice /c YN /n /m "Install the GCC toolchain now? [Y/N] "
if errorlevel 2 goto :cancelled
echo.
set "MSYSTEM=UCRT64"
set "CHERE_INVOKING=1"
"%MSYSROOT%\usr\bin\bash.exe" -lc "pacman -Sy --needed --noconfirm base-devel mingw-w64-ucrt-x86_64-toolchain"
set "PACMAN_RC=%ERRORLEVEL%"
echo.
call :try_bin "%MSYSROOT%\ucrt64\bin"
if defined CCBIN goto :compiler_found
echo [ERROR] GCC was not installed. pacman exit code: %PACMAN_RC%
echo         Open "MSYS2 UCRT64" from the Start menu and run:
echo             pacman -Syu
echo             pacman -S --needed base-devel mingw-w64-ucrt-x86_64-toolchain
echo         then run this builder again.
exit /b 1

rem ---------------------------------------------------------------------
rem  Compiler ready: put its folder on PATH and verify it runs
rem ---------------------------------------------------------------------
:compiler_found
set "GCC=%CCBIN%\gcc.exe"
set "GXX=%CCBIN%\g++.exe"
set "PATH=%CCBIN%;%PATH%"
echo [OK] Compiler folder: "%CCBIN%"
echo [OK] Added to PATH for this window.
echo.
echo [CHECK] g++ --version
"%GXX%" --version
if errorlevel 1 goto :gxx_broken
echo [OK] GCC / G++ are working.
echo.
goto :ask_source

:gxx_broken
echo [ERROR] g++ was found but could not run: "%GXX%"
echo         Reinstall the toolchain from the "MSYS2 UCRT64" terminal:
echo             pacman -S --needed base-devel mingw-w64-ucrt-x86_64-toolchain
exit /b 1

rem ---------------------------------------------------------------------
rem  Step 4a: ask for the source file
rem ---------------------------------------------------------------------
:ask_source
set "SRC="
if not defined C2E_DROP goto :prompt_source
set "SRC=%C2E_DROP%"
set "C2E_DROP="
echo [INFO] Using the dropped file.
goto :got_source

:prompt_source
echo Enter the C/C++ file: a name like boss.cpp, or a full path.
echo You can also drag the file into this window, then press Enter.
set /p "SRC=Source file: "

:got_source
call :clean SRC
if not defined SRC goto :src_empty
call :has_bad SRC
if "%C2E_BAD%"=="1" goto :src_badchars
set "SRCFULL="
call :find_src "%BATDIR%"
if not defined SRCFULL call :find_src "%ORIGDIR%"
if not defined SRCFULL goto :src_notfound
for %%F in ("%SRCFULL%") do set "EXT=%%~xF"
for %%F in ("%SRCFULL%") do set "SRCDIR=%%~dpF"
for %%F in ("%SRCFULL%") do set "SRCNAME=%%~nF"
if /i "%EXT%"==".c" goto :use_gcc
if /i "%EXT%"==".cpp" goto :use_gxx
if /i "%EXT%"==".cc" goto :use_gxx
if /i "%EXT%"==".cxx" goto :use_gxx
if /i "%EXT%"==".c++" goto :use_gxx
echo [ERROR] "%EXT%" is not a C/C++ file. Use .c, .cpp, .cc or .cxx
echo.
goto :ask_source

:src_empty
echo [ERROR] Please type a file name.
echo.
goto :ask_source

:src_badchars
echo [ERROR] The name has a character Windows does not allow: * ? ^< ^> ^|
echo.
goto :ask_source

:src_notfound
echo [ERROR] File not found: "%SRC%"
echo         Looked in: "%BATDIR%"
echo         and in:    "%ORIGDIR%"
echo         Tip: drag the file into this window to paste its full path.
echo.
goto :ask_source

:use_gcc
set "COMPILER=%GCC%"
set "LANGNAME=C"
goto :src_ok

:use_gxx
set "COMPILER=%GXX%"
set "LANGNAME=C++"

:src_ok
echo [OK] %LANGNAME% source: "%SRCFULL%"

rem ---------------------------------------------------------------------
rem  Step 4b: ask for the output EXE name
rem ---------------------------------------------------------------------
:ask_output
set "OUTNAME="
echo.
echo Output EXE name or path. Just press Enter to use "%SRCNAME%.exe".
echo A plain name is saved in the same folder as the source file.
set /p "OUTNAME=Output EXE: "
call :clean OUTNAME
if not defined OUTNAME set "OUTNAME=%SRCNAME%"
call :has_bad OUTNAME
if "%C2E_BAD%"=="1" goto :out_badchars
set "OUTNAME=%OUTNAME:/=\%"
if "%OUTNAME:~-1%"=="\" goto :out_isdir
if /i "%OUTNAME:~-4%"==".exe" set "OUTNAME=%OUTNAME:~0,-4%"
if not defined OUTNAME goto :out_invalid
if "%OUTNAME:~-1%"=="\" goto :out_isdir
if "%OUTNAME:~-1%"=="." goto :out_invalid

set "OUTFILE="
pushd "%SRCDIR%" 2>nul
for %%F in ("%OUTNAME%.exe") do set "OUTFILE=%%~fF"
popd
if not defined OUTFILE goto :out_invalid
for %%F in ("%OUTFILE%") do set "OUTDIR=%%~dpF"
if exist "%OUTFILE%\" goto :out_isdir
if exist "%OUTDIR%" goto :out_dir_ok
echo [INFO] Creating folder "%OUTDIR%"
mkdir "%OUTDIR%" 2>nul
if not exist "%OUTDIR%" goto :out_nodir

:out_dir_ok
if not exist "%OUTFILE%" goto :compile
echo [WARN] "%OUTFILE%" already exists.
choice /c YN /n /m "Replace it? [Y/N] "
if errorlevel 2 goto :ask_output
del /f /q "%OUTFILE%" >nul 2>nul
if not exist "%OUTFILE%" goto :compile
echo [ERROR] Cannot replace it - the old EXE is probably still running.
echo         Close that program, or choose another name.
goto :ask_output

:out_badchars
echo [ERROR] The name has a character Windows does not allow: * ? ^< ^> ^|
goto :ask_output

:out_isdir
echo [ERROR] That is a folder. Add a file name, like MyApp.exe
goto :ask_output

:out_invalid
echo [ERROR] That output name is not valid. Try something like MyApp.exe
goto :ask_output

:out_nodir
echo [ERROR] Could not create the output folder. Choose another location.
goto :ask_output

rem ---------------------------------------------------------------------
rem  Step 4c: compile
rem ---------------------------------------------------------------------
:compile
echo.
echo [4/4] Compiling ... compiler messages appear below.
echo   Source  : "%SRCFULL%"
echo   Output  : "%OUTFILE%"
echo   Command : "%COMPILER%" "%SRCFULL%" -O2 -static -o "%OUTFILE%"
echo.
pushd "%SRCDIR%" 2>nul
"%COMPILER%" "%SRCFULL%" -O2 -static -o "%OUTFILE%"
set "COMPILE_RC=%ERRORLEVEL%"
popd
echo.
if not "%COMPILE_RC%"=="0" goto :compile_failed
if not exist "%OUTFILE%" goto :exe_missing

echo [SUCCESS] Executable created:
echo   "%OUTFILE%"
echo [INFO] Built with -static, so it runs on other PCs without extra DLLs.
echo.
choice /c YN /n /m "Run the EXE now? [Y/N] "
if errorlevel 2 goto :done
echo.
echo ---------------- program output ----------------
pushd "%OUTDIR%" 2>nul
"%OUTFILE%"
set "RUN_RC=%ERRORLEVEL%"
popd
echo.
echo ------------------------------------------------
echo [INFO] Program exited with code %RUN_RC%.

:done
echo.
echo Finished.
exit /b 0

:compile_failed
echo [ERROR] Compilation failed - exit code %COMPILE_RC%.
echo         Fix the errors shown above in your code and run this again.
exit /b 1

:exe_missing
echo [ERROR] The compiler said OK but the EXE is missing:
echo         "%OUTFILE%"
echo         Your antivirus may have removed it. Check its quarantine.
exit /b 1

:cancelled
echo [INFO] Cancelled.
exit /b 1


rem ======================================================================
rem  Helper routines
rem ======================================================================

rem Accept the given folder as the compiler folder if it has gcc.exe and g++.exe.
:try_bin
if defined CCBIN goto :eof
if not exist "%~1\g++.exe" goto :eof
if not exist "%~1\gcc.exe" goto :eof
set "CCBIN=%~f1"
goto :eof

rem Set MSYSROOT to the first MSYS2 install found.
:find_msys_root
set "MSYSROOT="
for %%D in ("C:\msys64" "C:\msys2" "D:\msys64" "%LOCALAPPDATA%\Programs\msys64" "%LOCALAPPDATA%\msys64" "%USERPROFILE%\scoop\apps\msys2\current") do call :try_root "%%~D"
goto :eof

:try_root
if defined MSYSROOT goto :eof
if exist "%~1\usr\bin\bash.exe" set "MSYSROOT=%~f1"
goto :eof

rem Look for SRC, as typed or with .cpp/.c/.cc/.cxx added, relative to the given folder.
rem Full paths work too, because the for-loop full-path modifier leaves them unchanged.
:find_src
pushd "%~1" 2>nul || goto :eof
for %%F in ("%SRC%") do if exist "%%~fF" if not exist "%%~fF\" set "SRCFULL=%%~fF"
if not defined SRCFULL for %%F in ("%SRC%.cpp") do if exist "%%~fF" set "SRCFULL=%%~fF"
if not defined SRCFULL for %%F in ("%SRC%.c") do if exist "%%~fF" set "SRCFULL=%%~fF"
if not defined SRCFULL for %%F in ("%SRC%.cc") do if exist "%%~fF" set "SRCFULL=%%~fF"
if not defined SRCFULL for %%F in ("%SRC%.cxx") do if exist "%%~fF" set "SRCFULL=%%~fF"
popd
goto :eof

rem Remove all quotes and leading/trailing spaces from the named variable.
rem Uses delayed expansion so & ( ) ^ etc. in paths cannot break the script.
:clean
setlocal EnableDelayedExpansion
set "v=!%~1!"
if not defined v goto :clean_out
set "v=!v:"=!"
:clean_lead
if not defined v goto :clean_out
if "!v:~0,1!"==" " set "v=!v:~1!" & goto :clean_lead
:clean_trail
if not defined v goto :clean_out
if "!v:~-1!"==" " set "v=!v:~0,-1!" & goto :clean_trail
:clean_out
endlocal & set "%~1=%v%"
goto :eof

rem Set C2E_BAD=1 if the named variable has * ? < > | or a misplaced colon.
:has_bad
setlocal EnableDelayedExpansion
set "v=!%~1!"
set "b=0"
if not "!v!"=="!v:<=!" set "b=1"
if not "!v!"=="!v:>=!" set "b=1"
if not "!v!"=="!v:|=!" set "b=1"
if not "!v!"=="!v:?=!" set "b=1"
if not "!v!"=="!v:**=!" set "b=1"
set "w=!v:~2!"
if defined w if not "!w!"=="!w::=!" set "b=1"
endlocal & set "C2E_BAD=%b%"
goto :eof
