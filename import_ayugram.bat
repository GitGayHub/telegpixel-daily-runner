@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   TelegPixel — Импорт аккаунтов из AyuGram Desktop
echo ========================================================
python cli.py import-ayugram
echo.
python cli.py list-accounts
echo.
pause
