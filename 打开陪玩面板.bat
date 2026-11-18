@echo off
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" -B "%~dp0panel.py"
) else (
  start "" pyw -3.11 -B "%~dp0panel.py"
)
