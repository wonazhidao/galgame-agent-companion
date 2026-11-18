@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -B "%~dp0vn.py" %*
) else (
  py -3.11 -B "%~dp0vn.py" %*
)
exit /b %errorlevel%
