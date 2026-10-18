@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo ERROR: .venv\Scripts\pythonw.exe was not found.
    echo Please create or repair the project virtual environment first.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0gui.pyw"
exit /b 0
