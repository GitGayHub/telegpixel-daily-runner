@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   TelegPixel — Добавление аккаунта (QR или Телефон)
echo ========================================================
python cli.py add-account
echo.
python cli.py list-accounts
echo.
pause
