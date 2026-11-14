@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 main.py
goto finished
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python main.py
:finished
if errorlevel 1 pause
exit /b
:missing
echo Python 3 was not found. Install Python 3 and enable the Python launcher or PATH.
pause
exit /b 1
