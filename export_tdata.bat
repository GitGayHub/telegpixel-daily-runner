@echo off
chcp 65001 >nul
title TelegPixel - Экспорт архива tdata
echo ===================================================================
echo     TelegPixel — Экспорт архива сессий tdata для AyuGram
echo ===================================================================
echo.
echo 1. Создать архив и отправить в личный Telegram? [Y/n]
set /p SEND_TG="Ваш выбор (по умолчанию Y): "
if "%SEND_TG%"=="" set SEND_TG=Y

if /i "%SEND_TG%"=="Y" (
    python cli.py export-tdata --send-telegram
) else (
    python cli.py export-tdata
)

echo.
echo ===================================================================
echo Архив сохранён в data\ayugram_sessions_backup.zip
echo Нажмите любую клавишу для выхода...
pause >nul
