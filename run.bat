@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo    liveryAutoPackge  涂装自动打包工具  正在启动...
echo ============================================================

rem --- 逐个尝试候选 Python，取第一个版本>=3.9 且能运行的 ---
set "PY="

call :try_python python
if defined PY goto :have_py

call :try_python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto :have_py
call :try_python "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :have_py
call :try_python "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto :have_py
call :try_python "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY goto :have_py

call :try_python "%USERPROFILE%\miniconda3\python.exe"
if defined PY goto :have_py
call :try_python "%USERPROFILE%\anaconda3\python.exe"
if defined PY goto :have_py

call :try_python "C:\Python313\python.exe"
if defined PY goto :have_py
call :try_python "C:\Python312\python.exe"
if defined PY goto :have_py
call :try_python "C:\Python311\python.exe"
if defined PY goto :have_py

:have_py
if not defined PY (
  echo.
  echo [错误] 未找到可用的 Python 3.9+。
  echo 请安装 Python 后重新运行本脚本：https://www.python.org/downloads/
  echo （安装时可勾选 "Add python.exe to PATH"）
  echo 也可以手动把本机 Python 的路径加入上方候选列表。
  echo.
  pause
  exit /b 1
)

echo 使用 Python: !PY!
echo 启动服务后会自动打开浏览器；如未打开，请手动访问 http://127.0.0.1:8655/
echo 关闭本窗口即退出服务。
echo ============================================================
"!PY!" server.py
if errorlevel 1 (
  echo.
  echo 服务启动失败或已退出。可尝试命令：!PY! server.py --port 9000
)
pause
endlocal
exit /b 0

rem ============================================================
rem  子程序：尝试某个候选，能跑且版本>=3.9 则设置 PY
rem ============================================================
:try_python
set "CAND=%~1"
if "%CAND%"=="" goto :eof
"%CAND%" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%CAND%"
goto :eof
