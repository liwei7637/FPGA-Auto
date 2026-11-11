@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0..\.venv\Scripts\python.exe" goto local_venv
if exist "%~dp0..\runtime\python.exe" goto bundled
python -B -X utf8 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul
if not errorlevel 1 goto system_python
py -3 -B -X utf8 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul
if not errorlevel 1 goto python_launcher
echo Python 3.8+ is required for the build CLI. Install Python and reopen this terminal. 1>&2
exit /b 1
:local_venv
"%~dp0..\.venv\Scripts\python.exe" -B -X utf8 %*
exit /b %errorlevel%
:bundled
"%~dp0..\runtime\python.exe" -B -X utf8 %*
exit /b %errorlevel%
:system_python
python -B -X utf8 %*
exit /b %errorlevel%
:python_launcher
py -3 -B -X utf8 %*
exit /b %errorlevel%
