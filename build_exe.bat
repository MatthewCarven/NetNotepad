@echo off
REM ----------------------------------------------------------------------
REM Build netnotepad.exe via PyInstaller.
REM
REM Output: dist\netnotepad.exe   (single self-contained file, ~15-25MB)
REM
REM Drop that .exe anywhere on your PATH and you can launch netnotepad
REM from any cmd / Run dialog / Start menu search.
REM ----------------------------------------------------------------------

cd /d %~dp0

echo [1/4] Ensuring build deps are installed...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller regex zeroconf
if errorlevel 1 (
    echo Failed to install build dependencies.
    pause
    exit /b 1
)

echo [2/4] Cleaning previous build artifacts...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist netnotepad.spec del /q netnotepad.spec

echo [3/4] Running PyInstaller...
REM --onefile   : bundle everything into one .exe (slower startup, easier to ship)
REM --windowed  : no console window pops up alongside the Tk window
REM --name      : sets the output filename
REM --noconfirm : don't prompt before overwriting old build artifacts
REM --collect-all zeroconf : pull in all of zeroconf's submodules (some are
REM                          imported dynamically and PyInstaller's static
REM                          analyzer misses them otherwise)
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name netnotepad ^
    --noconfirm ^
    --collect-all zeroconf ^
    run.py
if errorlevel 1 (
    echo PyInstaller failed.
    pause
    exit /b 1
)

echo [4/4] Done.
echo.
echo Output: %~dp0dist\netnotepad.exe
echo.
echo To put it on your PATH:
echo   * personal tools dir (recommended):   mkdir %%USERPROFILE%%\bin ^&^& copy dist\netnotepad.exe %%USERPROFILE%%\bin\
echo     then add %%USERPROFILE%%\bin to your user PATH via System Properties.
echo   * or copy directly to anywhere already on PATH.
echo.
pause
