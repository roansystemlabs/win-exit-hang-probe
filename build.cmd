@echo off
rem Builds build\exit_hang_repro.exe (the optional controls) with MSVC. The probe itself is
rem plain Python and needs no build. Run from a "x64 Native Tools Command Prompt", or let this
rem script find vcvars64.bat through vswhere.
setlocal
cd /d "%~dp0"
where cl >nul 2>nul
if not errorlevel 1 goto build
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -products * -latest -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT goto nomsvc
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul || exit /b 1

:build
if not exist build mkdir build
cl /nologo /O1 /GS- /W4 repro\exit_hang_repro.c /Fobuild\ /Febuild\exit_hang_repro.exe /link /ENTRY:start /NODEFAULTLIB /SUBSYSTEM:CONSOLE kernel32.lib || exit /b 1
echo built build\exit_hang_repro.exe
exit /b 0

:nomsvc
echo MSVC not found. Install Visual Studio Build Tools with the C++ workload.
exit /b 1
