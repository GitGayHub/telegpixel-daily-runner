# 🎓 TelegPixel — Авто-чекин для Gemini Pro Automation Bot

Автоматический ежедневный сбор монеток (`/checkin`) со всех твоих Telegram-аккаунтов в боте **Gemini Pro Automation Bot 🎓** (`@sheeridverifier_bot`) и отправка красивой сводки балансов в твой бот **@pixelfarmik143714bot** — полностью автономно через **GitHub Actions** (по аналогии с `ThundrCoin`).

## 🚀 Как это работает
1. Каждые **8 часов** (`00:15`, `08:15`, `16:15 UTC` — `01:15`, `09:15`, `17:15` по Берлину, либо вручную кнопкой **Run workflow**) GitHub Actions запускает [`daily.yml`](.github/workflows/daily.yml).
2. Скрипт заходит на каждый подключённый Telegram-аккаунт через отдельную облачную `StringSession` (не выбивая твой AyuGram на ПК!).
3. Пишет `/checkin` в `@sheeridverifier_bot`, а если чекин сегодня уже был сделан — запрашивает точный баланс через `/me`.
4. Присылает в `@pixelfarmik143714bot` сводку: сколько монеток на каждом аккаунте, сколько в сумме, и какие аккаунты уже накопили на `/verify` (30+) или `/pixel` (50+).

## ➕ Управление аккаунтами на ПК
- **`restore_ayugram.bat`** (`python cli.py restore-tdata`) — восстанавливает ВСЕ аккаунты прямо в AyuGram на ПК в 1 клик.
- **`export_tdata.bat`** (`python cli.py export-tdata --send-telegram`) — упаковывает все аккаунты в готовый ZIP-архив с tdata и отправляет в личный Telegram.
- **`detach_ayugram.bat`** (`python cli.py detach-ayugram`) — отвязывает аккаунт локально из AyuGram на ПК без логаута (сессия в облаке остаётся живой).
- **`import_ayugram.bat`** (`python cli.py import-ayugram`) — автоматически подтягивает все аккаунты из `Documents\AyuGram\tdata`, создаёт для них независимые облачные сессии и сразу загружает их в секреты GitHub Actions.
- **`add_account.bat`** (`python cli.py add-account`) — добавляет любой аккаунт с телефона по **QR-коду прямо в консоли** или по номеру телефона и коду, и сразу синхронизирует с GitHub Actions.
- **`run_now.bat`** (`python cli.py run`) — запустить `/checkin` прямо сейчас локально.
- **`stats_now.bat`** (`python cli.py stats`) — проверить баланс монеток на всех аккаунтах без чекина.
- **`python cli.py get-code <имя>`** — получить 5-значный код входа из Telegram при утере SIM-карты.
- **`python cli.py qr-login <имя> <токен>`** — моментально подтвердить вход нового устройства по QR-ссылке без SIM.
- **`python cli.py list-accounts`** — посмотреть список добавленных аккаунтов.
- **`python cli.py sync-secrets`** — принудительно обновить секреты в GitHub Actions.

## 📦 Восстановление сессий в AyuGram / Telegram
Если вы переустановили Windows, сменили ПК или хотите вернуть все аккаунты обратно в AyuGram:
1. **Через Telegram-бот `@pixelfarmik143714bot`:**
   - Отправьте боту команду `/tdata`
   - Бот сгенерирует и пришлёт вам ZIP-архив прямо в личные сообщения
   - Распакуйте архив и запустите `RESTORE_TO_AYUGRAM.bat` — все аккаунты мгновенно появятся в AyuGram!
2. **Через GitHub Actions:**
   - Зайдите во вкладку **Actions** -> **Daily Check-in Runner** -> **Run workflow**
   - Выберите действие `export-tdata`
   - Архив прилетит вам в личный Telegram и появится во вкладке Artifacts запуска с AES-256 шифрованием.
3. **Локально на ПК:**
   - Просто запустите `restore_ayugram.bat` — он возьмёт сессии из `data/accounts.json` и сразу перенесёт их в `Documents\AyuGram\tdata`.

