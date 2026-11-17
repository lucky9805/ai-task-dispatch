@echo off
REM 一键启动常驻接活（Windows）
REM 用法： start.bat            轮询间隔 5 秒
REM        start.bat 2          轮询间隔 2 秒
REM        start.bat 5 echo     指定执行器
cd /d "%~dp0"

set INTERVAL=%1
if "%INTERVAL%"=="" set INTERVAL=5
set EXEC=%2

set ARGS=start --interval %INTERVAL%
if not "%EXEC%"=="" set ARGS=%ARGS% --executor %EXEC%
if "%3"=="--dry-run" set ARGS=%ARGS% --dry-run
if "%3"=="--once" set ARGS=%ARGS% --once

echo ==^> python agent.py %ARGS%
python agent.py %ARGS%
pause
