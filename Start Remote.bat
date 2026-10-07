@echo off
rem Double-click this file on the PC to start the phone remote.
cd /d "%~dp0"
title Phone Remote

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python is not installed yet.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo then double-click this file again.
  pause
  exit /b 1
)

echo Checking the three helper packages (first run takes a minute)...
%PY% -m pip install --quiet --disable-pip-version-check aiohttp pynput qrcode
if errorlevel 1 (
  echo Could not install the helper packages. Check the internet connection and try again.
  pause
  exit /b 1
)

%PY% phone_remote.py %*
pause
