@echo off
chcp 65001 >nul
title TelegPixel - Восстановление сессий в AyuGram
echo ===================================================================
echo     TelegPixel — Восстановление всех аккаунтов в AyuGram на ПК
echo ===================================================================
echo.
echo Этот скрипт:
echo  1. Закроет AyuGram (если запущен)
echo  2. Сделает резервную копию текущего tdata
echo  3. Восстановит ВСЕ аккаунты из data/accounts.json прямо в AyuGram
echo.
echo Нажмите любую клавишу для запуска...
pause >nul

python cli.py restore-tdata --close-ayugram

echo.
echo ===================================================================
echo Нажмите любую клавишу для выхода...
pause >nul
