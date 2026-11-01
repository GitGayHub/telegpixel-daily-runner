@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo =================================================================
echo   TelegPixel — Убрать аккаунт из AyuGram БЕЗ сброса сессии бота
echo =================================================================
python cli.py detach-ayugram
echo.
pause
