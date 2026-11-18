@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" goto install
py -3.11 -V >nul 2>&1
if errorlevel 1 goto use_python
py -3.11 -m venv "%~dp0.venv"
if errorlevel 1 goto failed
goto install
:use_python
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 11))"
if errorlevel 1 goto failed
python -m venv "%~dp0.venv"
if errorlevel 1 goto failed
:install
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
echo Ready.
if /i not "%~1"=="--agent" pause
exit /b 0
:failed
echo Setup failed. Check Windows Python 3.11 and network access.
if /i not "%~1"=="--agent" pause
exit /b 1
