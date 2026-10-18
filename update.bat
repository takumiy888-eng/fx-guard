@echo off
setlocal
rem 自分自身を上書きしないよう、一時フォルダにコピーしてそちらで実行する
if /i not "%~1"=="--from-temp" (
  copy /y "%~f0" "%TEMP%\fxguard_updater.bat" >nul
  "%TEMP%\fxguard_updater.bat" --from-temp "%~dp0"
)
set "APPDIR=%~2"
cd /d "%APPDIR%"
title FX Guard update

powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',8080); exit 0 } catch { exit 1 } finally { $c.Close() }" >nul 2>nul
if not errorlevel 1 (
  echo FX Guard が動いています。黒いウィンドウを閉じてから、もう一度 update.bat を実行してください。
  pause
  exit /b 1
)

echo 最新版をダウンロードしています...
set "TMPZ=%TEMP%\fxguard_update.zip"
set "TMPD=%TEMP%\fxguard_update"
if exist "%TMPD%" rmdir /s /q "%TMPD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing 'https://github.com/takumiy888-eng/fx-guard/archive/refs/heads/main.zip' -OutFile $env:TMPZ; Expand-Archive -Path $env:TMPZ -DestinationPath $env:TMPD -Force"
if errorlevel 1 goto fail
if not exist "%TMPD%\fx-guard-main\run_local.py" goto fail
robocopy "%TMPD%\fx-guard-main" "%APPDIR%." /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto fail
del "%TMPZ%" >nul 2>nul
rmdir /s /q "%TMPD%" >nul 2>nul
echo 更新しました。取引履歴・学習結果・設定はそのままです。
echo FX Guard を起動するには start.bat をダブルクリックしてください。
pause
exit /b 0

:fail
echo 更新に失敗しました。インターネット接続を確認して、もう一度実行してください。
pause
exit /b 1
