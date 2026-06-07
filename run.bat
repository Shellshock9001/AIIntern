@echo off
REM ARGUS FinDash - one-command launcher (Windows)
REM Usage: run.bat  |  run.bat eval  |  run.bat ingest  |  run.bat doctor
setlocal

REM --- Find a Python launcher: try py, then python, then python3 ---
set "PYEXE="
where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py"
if defined PYEXE goto have_python

where python >nul 2>nul
if %errorlevel%==0 set "PYEXE=python"
if defined PYEXE goto have_python

where python3 >nul 2>nul
if %errorlevel%==0 set "PYEXE=python3"
if defined PYEXE goto have_python

echo.
echo Python was not found on your PATH.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then re-run run.bat
echo.
pause
exit /b 1

:have_python

REM --- Create the virtual environment on first run ---
if exist ".venv\Scripts\activate.bat" goto have_venv
echo Creating virtual environment...
%PYEXE% -m venv .venv
if %errorlevel%==0 goto have_venv
echo Failed to create virtualenv. Is Python 3.10+ installed?
pause
exit /b 1

:have_venv
call ".venv\Scripts\activate.bat"

REM --- Install dependencies only if Streamlit isn't importable yet ---
python -c "import streamlit" >nul 2>nul
if %errorlevel%==0 goto have_deps
echo Installing dependencies, one time...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if %errorlevel%==0 goto have_deps
echo Dependency install failed. See messages above.
pause
exit /b 1

:have_deps
python run.py %*

endlocal
