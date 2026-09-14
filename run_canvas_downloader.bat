@echo off
setlocal
set "PYTHONUTF8=1"
title Canvas Course File Downloader
set "SCRIPT_PATH=%~dp0download_canvas_course_files.py"

if not exist "%SCRIPT_PATH%" goto missing_script

where python >nul 2>nul
if errorlevel 1 goto use_py
python "%SCRIPT_PATH%" %*
goto finished

:use_py
where py >nul 2>nul
if errorlevel 1 goto no_python
py -3 "%SCRIPT_PATH%" %*
goto finished

:missing_script
echo.
echo ERROR: download_canvas_course_files.py was not found next to this launcher.
set "RUN_RESULT=1"
goto pause_and_exit

:no_python
echo.
echo ERROR: Python 3 was not found. Install Python 3 and enable Add Python to PATH.
set "RUN_RESULT=1"
goto pause_and_exit

:finished
set "RUN_RESULT=%ERRORLEVEL%"

:pause_and_exit
echo.
pause
endlocal & exit /b %RUN_RESULT%
