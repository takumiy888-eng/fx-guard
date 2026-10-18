@echo off
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\FXGuard.cmd"
> "%TARGET%" echo @echo off
>> "%TARGET%" echo cd /d "%~dp0"
>> "%TARGET%" echo start "FX Guard" /min "%~dp0start.bat" --no-browser
if exist "%TARGET%" (
  echo 自動起動をONにしました。PCを起動してログインすると、FX Guard が最小化された状態で動き始めます。
) else (
  echo 自動起動を設定できませんでした。
)
pause
