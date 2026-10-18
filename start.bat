@echo off
setlocal
cd /d "%~dp0"
title FX Guard
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" goto install

echo.
echo [初回セットアップ] Python を探しています...
set "PY="
for /f "delims=" %%i in ('py -3 tools\find_python.py 2^>nul') do set "PY=%%i"
if defined PY goto mkvenv
for /f "delims=" %%i in ('python tools\find_python.py 2^>nul') do set "PY=%%i"
if defined PY goto mkvenv
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto mkvenv

echo Python が見つからないので、自動でインストールします（数分かかります）...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto mkvenv

echo.
echo Python を自動でインストールできませんでした。
echo https://www.python.org/downloads/ から Python をインストールして、
echo もう一度 start.bat をダブルクリックしてください。
echo （インストール画面で「Add python.exe to PATH」にチェックを入れてください）
pause
exit /b 1

:mkvenv
echo Python: %PY%
echo 専用の環境を作っています...
"%PY%" -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo 環境を作れませんでした。フォルダを デスクトップ などに移してから再実行してください。
  pause
  exit /b 1
)

:install
echo 必要なライブラリを確認しています...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo ライブラリのインストールに失敗しました。インターネット接続を確認して、もう一度実行してください。
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run_local.py %*
echo.
echo FX Guard が停止しました。
pause
