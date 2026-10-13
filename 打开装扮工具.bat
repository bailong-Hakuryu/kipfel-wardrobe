@echo off
rem Kipfel wardrobe launcher: starts the local service without a console window.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0wardrobe.py"
    exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" /min python "%~dp0wardrobe.py"
    exit /b
)
echo Python 3 was not found. Please install Python 3 from https://www.python.org/ and try again.
pause
