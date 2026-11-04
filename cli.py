#!/usr/bin/env python3
"""
Command Line Interface (CLI) for TelegPixel Daily Check-in & Coin Tracker.
Automates daily /checkin in Gemini Pro Automation Bot (@sheeridverifier_bot)
and sends per-account coin summaries via Telegram Bot API (@pixelfarmik143714bot).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import telegpixel.env_loader  # noqa: E402, F401

import click  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.logging import RichHandler  # noqa: E402
from rich.table import Table  # noqa: E402

from telegpixel.accounts import AccountManager, AccountRecord  # noqa: E402
from telegpixel.github_sync import detect_github_repo, sync_secrets_to_github  # noqa: E402
from telegpixel.notifier import TelegramNotifier  # noqa: E402
from telegpixel.parser import CheckinStatus  # noqa: E402
from telegpixel.runner import AccountRunResult, CheckinRunner  # noqa: E402

console = Console()
logger = logging.getLogger("telegpixel.cli")


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)


def _print_results_table(results: list[AccountRunResult]) -> None:
    table = Table(title="🎓 Gemini Pro Automation Bot — Сводка аккаунтов")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Аккаунт", style="cyan", no_wrap=True)
    table.add_column("Статус", justify="center")
    table.add_column("За сегодня", justify="right", style="green")
    table.add_column("Баланс (монет)", justify="right", style="bold yellow")
    table.add_column("USD", justify="right")
    table.add_column("Серия", justify="right")

    for i, r in enumerate(results, start=1):
        if r.status == CheckinStatus.CLAIMED:
            st_str = "[bold green]✅ CLAIMED[/bold green]"
        elif r.status == CheckinStatus.COOLDOWN:
            st_str = "[yellow]⏳ COOLDOWN[/yellow]"
        elif r.status == CheckinStatus.CHECKED:
            st_str = "[blue]🔎 CHECKED[/blue]"
        else:
            st_str = f"[bold red]❌ FAILED[/bold red] ({(r.error_message or '')[:28]})"

        earned_str = f"+{r.coins_earned}" if r.coins_earned > 0 else "0"
        bal_str = str(r.balance) if r.balance is not None else "?"
        usd_str = f"${r.usd_value:.2f}" if r.usd_value is not None else "-"
        streak_str = f"{r.streak_days} дн." if r.streak_days else "-"
        table.add_row(str(i), r.account.display_name, st_str, earned_str, bal_str, usd_str, streak_str)

    console.print(table)


def _auto_sync_github_if_possible(mgr: AccountManager) -> None:
    repo = detect_github_repo()
    if not repo:
        return
    console.print(f"[cyan]🔄 Синхронизация аккаунтов с GitHub Actions ({repo})...[/cyan]")
    secrets_map = {
        "TELEGPIXEL_ACCOUNTS_JSON": mgr.export_secret_json(),
    }
    bot_token = (os.getenv("TELEGPIXEL_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGPIXEL_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if bot_token:
        secrets_map["TELEGPIXEL_BOT_TOKEN"] = bot_token
        secrets_map["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        secrets_map["TELEGPIXEL_CHAT_ID"] = chat_id
        secrets_map["TELEGRAM_CHAT_ID"] = chat_id

    try:
        outcomes = sync_secrets_to_github(secrets_map, repo=repo)
        ok_all = all(outcomes.values())
        if ok_all:
            console.print(f"[bold green]✅ Секреты GitHub успешно обновлены ({', '.join(outcomes.keys())})![/bold green]")
        else:
            console.print(f"[yellow]⚠️ Частичная синхронизация секретов: {outcomes}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]⚠️ Не удалось автоматически обновить секреты GitHub: {e}[/yellow]")


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Включить подробные debug-логи.")
def cli(verbose: bool) -> None:
    """🎓 TelegPixel — Авто-чекин и учёт монеток в Gemini Pro Automation Bot."""
    configure_logging(verbose)


@cli.command("run")
@click.option("-a", "--account", "account_name", default=None, help="Запустить только для одного аккаунта.")
@click.option("--dry-run", is_flag=True, help="Проверить баланс (/me) без отправки /checkin.")
@click.option("--no-telegram", is_flag=True, help="Не отправлять итоговый отчёт в Telegram-бота.")
@click.option(
    "--smart-notify",
    is_flag=True,
    help="Отправлять отчёт только если получены монеты, есть ошибка или прошло >20ч (для частых cron-прогонов).",
)
def cmd_run(account_name: Optional[str], dry_run: bool, no_telegram: bool, smart_notify: bool) -> None:
    """Выполнить ежедневный /checkin на всех аккаунтах и прислать отчёт."""
    mgr = AccountManager()
    notifier = TelegramNotifier()
    accounts = mgr.list_accounts(enabled_only=True)

    if not no_telegram:
        notifier.process_pending_commands(accounts)

    if account_name:
        needle = account_name.strip().lstrip("@").lower()
        accounts = [
            a
            for a in accounts
            if a.username.lower() == needle or str(a.user_id) == needle
        ]
        if not accounts:
            console.print(f"[bold red]❌ Аккаунт '{account_name}' не найден.[/bold red]")
            sys.exit(1)

    if not accounts:
        console.print(
            "[yellow]⚠️ Нет подключённых аккаунтов! Запустите `import_ayugram.bat` или `add_account.bat`.[/yellow]"
        )
        sys.exit(1)

    console.print(f"[bold cyan]🚀 Запуск TelegPixel для {len(accounts)} аккаунта(ов)...[/bold cyan]")
    runner = CheckinRunner()
    results = asyncio.run(runner.run_all(accounts, dry_run=dry_run, stats_only=False))
    mgr.save_state()

    _print_results_table(results)

    if not no_telegram and notifier.should_send_report(results, smart_notify=smart_notify):
        report_html = notifier.format_report(results, dry_run=dry_run)
        sent_ok = notifier.send_message(report_html, mark_report_sent=True)
        if not sent_ok:
            if asyncio.run(runner.send_report_to_saved_messages(accounts, report_html, notifier.chat_id)):
                notifier._save_bot_state(
                    {**notifier._load_bot_state(), "last_report_sent_ts": __import__("time").time()}
                )

    # Fail CI step only if ALL accounts failed
    if results and all(r.status == CheckinStatus.FAILED for r in results):
        console.print("[bold red]❌ Все аккаунты завершились с ошибкой![/bold red]")
        sys.exit(1)


@cli.command("stats")
@click.option("--cached", is_flag=True, help="Использовать сохранённый баланс без подключения к Telegram.")
@click.option("--no-telegram", is_flag=True, help="Не отправлять отчёт в Telegram-бота.")
def cmd_stats(cached: bool, no_telegram: bool) -> None:
    """Проверить сколько монеток на каждом аккаунте (через /me) без чекина."""
    mgr = AccountManager()
    notifier = TelegramNotifier()
    accounts = mgr.list_accounts(enabled_only=True)

    if not accounts:
        console.print("[yellow]⚠️ Нет подключённых аккаунтов![/yellow]")
        sys.exit(1)

    if cached:
        results = [
            AccountRunResult(
                account=a,
                status=CheckinStatus.CHECKED,
                balance=a.balance,
                usd_value=a.usd_value,
                streak_days=a.streak_days,
            )
            for a in accounts
        ]
    else:
        runner = CheckinRunner()
        results = asyncio.run(runner.run_all(accounts, stats_only=True))
        mgr.save_state()

    _print_results_table(results)
    if not no_telegram:
        report_html = notifier.format_report(results, stats_only=True)
        if not notifier.send_message(report_html):
            asyncio.run(CheckinRunner().send_report_to_saved_messages(accounts, report_html, notifier.chat_id))


@cli.command("list-accounts")
def cmd_list_accounts() -> None:
    """Показать список подключённых Telegram-аккаунтов."""
    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    if not accounts:
        console.print("[yellow]Список аккаунтов пуст.[/yellow]")
        return

    table = Table(title=f"Подключённые аккаунты ({len(accounts)})")
    table.add_column("#", justify="right")
    table.add_column("Аккаунт", style="cyan")
    table.add_column("User ID")
    table.add_column("Имя")
    table.add_column("Баланс", justify="right", style="bold yellow")
    table.add_column("Последний чекин")
    for idx, acc in enumerate(accounts, start=1):
        bal = f"{acc.balance} монет" if acc.balance is not None else "—"
        table.add_row(
            str(idx),
            acc.display_name,
            str(acc.user_id),
            acc.first_name or "—",
            bal,
            acc.last_checkin_at or "—",
        )
    console.print(table)


@cli.command("import-ayugram")
@click.option(
    "--tdata",
    "tdata_dir",
    default=str(Path.home() / "Documents" / "AyuGram" / "tdata"),
    show_default=True,
    help="Путь к папке tdata клиента AyuGram / Telegram Desktop.",
)
@click.option("--password", "default_2fa", default=None, help="Облачный пароль 2FA (если одинаковый на аккаунтах).")
@click.option("--force", is_flag=True, help="Пересоздать сессии даже для уже добавленных аккаунтов.")
@click.option("--no-sync", is_flag=True, help="Не синхронизировать секреты в GitHub автоматически.")
def cmd_import_ayugram(tdata_dir: str, default_2fa: Optional[str], force: bool, no_sync: bool) -> None:
    """Импортировать все аккаунты из папки AyuGram и создать облачные сессии."""
    from telegpixel.ayugram_importer import import_from_ayugram_tdata

    mgr = AccountManager()
    existing_ids = {a.user_id for a in mgr.list_accounts(enabled_only=False)}

    def prompt_2fa(display_name: str) -> Optional[str]:
        if default_2fa:
            return default_2fa
        console.print(f"\n[bold yellow]🔐 На аккаунте {display_name} включён облачный пароль (2FA).[/bold yellow]")
        pwd = click.prompt(f"Введите 2FA-пароль для {display_name} (или Enter для пропуска)", default="", show_default=False)
        return pwd.strip() or None

    console.print(f"[bold cyan]📂 Поиск аккаунтов в {tdata_dir}...[/bold cyan]")
    imported, skipped_2fa = asyncio.run(
        import_from_ayugram_tdata(
            tdata_path=Path(tdata_dir),
            existing_user_ids=existing_ids,
            password_provider=prompt_2fa,
            force_reimport=force,
        )
    )

    for rec in imported:
        mgr.upsert_account(rec)
        console.print(f"[bold green]✅ Добавлен аккаунт {rec.display_name} (ID: {rec.user_id})[/bold green]")

    if imported:
        mgr.save_accounts()
        if not no_sync:
            _auto_sync_github_if_possible(mgr)
    elif not skipped_2fa:
        console.print("[green]Все найденные в AyuGram аккаунты уже добавлены.[/green]")

    if skipped_2fa:
        console.print(
            f"[yellow]⚠️ Пропущено аккаунтов из-за 2FA пароля: {skipped_2fa}. "
            f"Запустите команду снова и введите 2FA-пароль.[/yellow]"
        )


@cli.command("add-account")
@click.option(
    "--method",
    type=click.Choice(["qr", "phone"], case_sensitive=False),
    default=None,
    help="Способ входа: QR-код с телефона или номер телефона + код.",
)
@click.option("--no-sync", is_flag=True, help="Не синхронизировать секреты в GitHub автоматически.")
def cmd_add_account(method: Optional[str], no_sync: bool) -> None:
    """Добавить новый Telegram-аккаунт (по QR-коду или номеру телефона)."""
    from telegpixel.ayugram_importer import login_account_via_phone, login_account_via_qr

    mgr = AccountManager()
    chosen = method
    if not chosen:
        console.print("\n[bold cyan]Выберите способ добавления аккаунта:[/bold cyan]")
        console.print("  [bold]1[/bold] — По QR-коду (быстро с телефона: Настройки -> Устройства)")
        console.print("  [bold]2[/bold] — По номеру телефона и коду из Telegram/SMS")
        choice = click.prompt("Ваш выбор", type=click.Choice(["1", "2"]), default="1")
        chosen = "qr" if choice == "1" else "phone"

    def prompt_2fa(label: str) -> Optional[str]:
        pwd = click.prompt(f"Введите облачный 2FA-пароль для {label}", default="", show_default=False)
        return pwd.strip() or None

    if chosen == "qr":
        rec = asyncio.run(login_account_via_qr(password_provider=prompt_2fa, print_fn=console.print))
    else:
        phone = click.prompt("Введите номер телефона (например +380... или +49...)").strip()
        rec = asyncio.run(
            login_account_via_phone(
                phone=phone,
                code_provider=lambda: click.prompt("Введите код из Telegram / SMS"),
                password_provider=prompt_2fa,
            )
        )

    if rec:
        mgr.upsert_account(rec)
        mgr.save_accounts()
        console.print(f"[bold green]🎉 Аккаунт {rec.display_name} (ID: {rec.user_id}) успешно сохранён![/bold green]")
        if not no_sync:
            _auto_sync_github_if_possible(mgr)
    else:
        console.print("[red]❌ Не удалось авторизовать аккаунт.[/red]")


@cli.command("reauth")
@click.option("--all", "reauth_all", is_flag=True, help="Перепривязать ВСЕ аккаунты напрямую (независимо от ПК/AyuGram).")
@click.option("--password", "default_2fa", default=None, help="Облачный пароль 2FA (если одинаковый).")
@click.option(
    "--method",
    type=click.Choice(["qr", "phone"], case_sensitive=False),
    default="qr",
    show_default=True,
    help="Способ перепривязки без AyuGram: qr (скан с телефона — без антифрод-киков) или phone (код).",
)
@click.option("--no-sync", is_flag=True, help="Не синхронизировать секреты в GitHub автоматически.")
def cmd_reauth(reauth_all: bool, default_2fa: Optional[str], method: str, no_sync: bool) -> None:
    """Проверить сессии и переподключить аккаунты напрямую (без привязки к ПК/AyuGram)."""
    from telegpixel.ayugram_importer import (
        check_session_authorized,
        login_account_via_phone,
        login_account_via_qr,
    )

    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=not reauth_all)
    if not accounts:
        console.print("[yellow]⚠️ Нет аккаунтов в data/accounts.json.[/yellow]")
        return

    if reauth_all:
        expired = list(accounts)
        console.print(f"[bold cyan]🔄 Режим полной перепривязки всех {len(expired)} аккаунтов напрямую (без ПК)...[/bold cyan]")
    else:
        console.print("[bold cyan]🔍 Проверка активности сессий всех аккаунтов...[/bold cyan]")

        async def _check_all() -> list[AccountRecord]:
            expired_list: list[AccountRecord] = []
            for acc in accounts:
                alive = await check_session_authorized(acc)
                if alive:
                    console.print(f"  [bold green]✅ {acc.display_name}[/bold green] — сессия активна")
                else:
                    phone_hint = f" (+{acc.phone.lstrip('+')})" if acc.phone else ""
                    console.print(f"  [bold red]❌ {acc.display_name}{phone_hint}[/bold red] — сессия сброшена")
                    expired_list.append(acc)
            return expired_list

        expired = asyncio.run(_check_all())
        if not expired:
            console.print("\n[bold green]🎉 Все аккаунты активны! Перепривязка не требуется.[/bold green]")
            return

    console.print(
        f"\n[bold yellow]⚡ Найдено слетевших аккаунтов: {len(expired)}. "
        f"Сейчас создадим для них прямые (корневые) сессии — входить в AyuGram НЕ нужно![/bold yellow]"
    )

    env_2fa = (default_2fa or os.getenv("TELEGPIXEL_DEFAULT_2FA") or "").strip() or None

    updated_count = 0
    for acc in expired:
        tried_env_2fa = False

        def prompt_2fa(label: str) -> Optional[str]:
            nonlocal tried_env_2fa
            if env_2fa and not tried_env_2fa:
                tried_env_2fa = True
                return env_2fa
            pwd = click.prompt(f"Введите облачный 2FA-пароль для {label}", default="", show_default=False)
            return pwd.strip() or None
        phone_str = f"+{acc.phone.lstrip('+')}" if acc.phone else ""
        console.print(f"\n[bold cyan]➡️ Восстановление {acc.display_name} ({acc.first_name or acc.user_id})[/bold cyan]")
        use_method = method.lower()
        if use_method == "phone" and not phone_str:
            phone_str = click.prompt(f"Введите номер телефона для {acc.display_name} (+380... / +49...)").strip()

        if use_method == "qr":
            console.print(
                f"📱 Возьмите телефон с аккаунтом [bold]{acc.display_name}[/bold] "
                f"({phone_str or acc.user_id}) и отсканируйте QR:"
            )
            rec = asyncio.run(
                login_account_via_qr(
                    password_provider=prompt_2fa,
                    print_fn=console.print,
                    seed=phone_str or str(acc.user_id),
                )
            )
        else:
            console.print(f"📲 Отправляю код подтверждения на [bold]{phone_str}[/bold]...")
            rec = asyncio.run(
                login_account_via_phone(
                    phone=phone_str,
                    code_provider=lambda: click.prompt(f"Введите 5-значный код из Telegram/SMS для {acc.display_name}"),
                    password_provider=prompt_2fa,
                )
            )

        if rec:
            rec.last_error = None
            mgr.upsert_account(rec)
            mgr.save_accounts()
            mgr.save_state()
            updated_count += 1
            console.print(f"[bold green]✅ {rec.display_name} успешно восстановлен (прямая сессия)![/bold green]")

    if updated_count > 0 and not no_sync:
        _auto_sync_github_if_possible(mgr)


@cli.command("send-code")
@click.argument("target")
def cmd_send_code(target: str) -> None:
    """Шаг 1 прямого входа: отправить код на телефон или @username из базы."""
    from telegpixel.ayugram_importer import send_login_code

    mgr = AccountManager()
    needle = target.strip().lstrip("@").lower()
    phone = target.strip()
    label = target.strip()
    for acc in mgr.list_accounts(enabled_only=False):
        if acc.username.lower() == needle or str(acc.user_id) == needle or (acc.first_name or "").lower() == needle:
            if acc.phone:
                phone = f"+{acc.phone.lstrip('+')}"
                label = f"{acc.display_name} ({phone})"
            break

    delivery = asyncio.run(send_login_code(phone))
    console.print(f"[bold green]📲 Код отправлен для {label} (тип доставки: {delivery}).[/bold green]")


@cli.command("confirm-code")
@click.argument("target")
@click.argument("code")
@click.option("--password", "password_2fa", default=None, help="Облачный пароль 2FA (если включён).")
@click.option("--no-sync", is_flag=True, help="Не синхронизировать секреты в GitHub автоматически.")
def cmd_confirm_code(target: str, code: str, password_2fa: Optional[str], no_sync: bool) -> None:
    """Шаг 2 прямого входа: подтвердить 5-значный код и сохранить корневую сессию."""
    from telegpixel.ayugram_importer import complete_login_code

    mgr = AccountManager()
    needle = target.strip().lstrip("@").lower()
    phone = target.strip()
    for acc in mgr.list_accounts(enabled_only=False):
        if acc.username.lower() == needle or str(acc.user_id) == needle or (acc.first_name or "").lower() == needle:
            if acc.phone:
                phone = f"+{acc.phone.lstrip('+')}"
            break

    pwd = password_2fa or (os.getenv("TELEGPIXEL_DEFAULT_2FA") or "").strip() or None
    rec = asyncio.run(complete_login_code(phone=phone, code=code, password=pwd))
    rec.last_error = None
    mgr.upsert_account(rec)
    mgr.save_accounts()
    mgr.save_state()
    console.print(f"[bold green]🎉 Прямая сессия для {rec.display_name} (ID: {rec.user_id}) сохранена![/bold green]")
    if not no_sync:
        _auto_sync_github_if_possible(mgr)


@cli.command("detach-ayugram")
@click.option(
    "--tdata",
    "tdata_dir",
    default=str(Path.home() / "Documents" / "AyuGram" / "tdata"),
    show_default=True,
    help="Путь к папке tdata клиента AyuGram.",
)
@click.option(
    "-r",
    "--remove",
    "remove_targets",
    multiple=True,
    help="Кого убрать из AyuGram (@username, имя или user_id) без сброса облачной сессии.",
)
@click.option("--close-ayugram", "auto_close", is_flag=True, help="Автоматически закрыть AyuGram.exe перед правкой.")
def cmd_detach_ayugram(tdata_dir: str, remove_targets: tuple[str, ...], auto_close: bool) -> None:
    """Убрать аккаунт из AyuGram локально БЕЗ нажатия 'Выйти' (сессия бота НЕ слетает!)."""
    from telegpixel.ayugram_importer import (
        close_ayugram,
        detach_accounts_from_tdata,
        is_ayugram_running,
        read_tdata_slots,
    )

    tpath = Path(tdata_dir)
    mgr = AccountManager()
    known_by_uid = {a.user_id: a for a in mgr.list_accounts(enabled_only=False)}

    slots = read_tdata_slots(tpath)
    if not slots:
        console.print("[yellow]В папке AyuGram нет активных слотов.[/yellow]")
        return

    console.print("[bold cyan]📂 Сейчас в AyuGram на ПК открыты слоты:[/bold cyan]")
    for slot_idx, uid, dc, _ in slots:
        acc = known_by_uid.get(uid)
        label = f"{acc.display_name} ({acc.first_name or '—'})" if acc else f"ID:{uid}"
        console.print(f"  • Слот #{slot_idx}: [bold]{label}[/bold] [dim](ID: {uid}, DC{dc})[/dim]")

    uids_to_remove: set[int] = set()
    if remove_targets:
        for t in remove_targets:
            needle = t.strip().lstrip("@").lower()
            for _, uid, _, _ in slots:
                acc = known_by_uid.get(uid)
                if (
                    str(uid) == needle
                    or (acc and acc.username.lower() == needle)
                    or (acc and (acc.first_name or "").strip().lower() == needle)
                ):
                    uids_to_remove.add(uid)
    else:
        raw = click.prompt(
            "\nВведите @username, имя или User ID аккаунта(ов) через запятую, которые убрать из AyuGram на ПК"
        )
        for part in raw.split(","):
            needle = part.strip().lstrip("@").lower()
            if not needle:
                continue
            for _, uid, _, _ in slots:
                acc = known_by_uid.get(uid)
                if (
                    str(uid) == needle
                    or (acc and acc.username.lower() == needle)
                    or (acc and (acc.first_name or "").strip().lower() == needle)
                ):
                    uids_to_remove.add(uid)

    if not uids_to_remove:
        console.print("[yellow]Не выбрано ни одного совпавшего аккаунта из AyuGram.[/yellow]")
        return

    if is_ayugram_running():
        if auto_close or click.confirm("AyuGram сейчас запущен. Закрыть его на пару секунд для безопасной отвязки?", default=True):
            close_ayugram()
        else:
            console.print("[red]Отменено: сначала закройте AyuGram.[/red]")
            return

    removed = detach_accounts_from_tdata(tpath, uids_to_remove)
    console.print(
        f"[bold green]✅ Локально убрано из AyuGram слотов: {len(removed)} (ID: {removed}) — "
        f"облачные сессии для бота остались ЖИВЫМИ![/bold green]"
    )


@cli.command("remove-account")
@click.argument("identifier")
@click.option("--no-sync", is_flag=True, help="Не синхронизировать секреты в GitHub автоматически.")
def cmd_remove_account(identifier: str, no_sync: bool) -> None:
    """Удалить аккаунт по @username или user_id."""
    mgr = AccountManager()
    if mgr.remove_account(identifier):
        mgr.save_accounts()
        console.print(f"[green]✅ Аккаунт '{identifier}' удалён.[/green]")
        if not no_sync:
            _auto_sync_github_if_possible(mgr)
    else:
        console.print(f"[red]❌ Аккаунт '{identifier}' не найден.[/red]")


@cli.command("sync-secrets")
@click.option("--repo", default=None, help="GitHub репозиторий в формате owner/name.")
@click.option("--trigger", is_flag=True, help="Запустить тестовый прогон GitHub Actions после синхронизации.")
def cmd_sync_secrets(repo: Optional[str], trigger: bool) -> None:
    """Загрузить все локальные аккаунты и настройки бота в GitHub Actions Secrets."""
    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    if not accounts:
        console.print("[red]❌ Нет активных аккаунтов в data/accounts.json для выгрузки![/red]")
        sys.exit(1)

    secrets_map = {
        "TELEGPIXEL_ACCOUNTS_JSON": mgr.export_secret_json(),
    }
    bot_token = (os.getenv("TELEGPIXEL_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGPIXEL_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if bot_token:
        secrets_map["TELEGPIXEL_BOT_TOKEN"] = bot_token
        secrets_map["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        secrets_map["TELEGPIXEL_CHAT_ID"] = chat_id
        secrets_map["TELEGRAM_CHAT_ID"] = chat_id

    target_repo = repo or detect_github_repo()
    outcomes = sync_secrets_to_github(secrets_map, repo=target_repo)
    for k, ok in outcomes.items():
        mark = "[bold green]OK[/bold green]" if ok else "[bold red]FAIL[/bold red]"
        console.print(f"  • {k}: {mark}")

    if trigger and target_repo:
        console.print(f"[cyan]▶️ Запуск workflow daily.yml в {target_repo}...[/cyan]")
        subprocess.run(["gh", "workflow", "run", "daily.yml", "--repo", target_repo], check=False)


@cli.command("get-code")
@click.argument("target", required=False, default=None)
@click.option("--any-age", is_flag=True, help="Не ограничивать давность кода (показать последний найденный).")
@click.option("--no-telegram", is_flag=True, help="Не отправлять код в Telegram-бот.")
def cmd_get_code(target: Optional[str], any_age: bool, no_telegram: bool) -> None:
    """Получить код входа в Telegram для аккаунта БЕЗ физической SIM-карты."""
    import html
    from telegpixel.ayugram_importer import fetch_telegram_service_code

    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    if not accounts:
        console.print("[red]Нет доступных аккаунтов.[/red]")
        return

    selected = None
    if target:
        needle = target.strip().lstrip("@").lower()
        for a in accounts:
            if (
                a.username.lower() == needle
                or str(a.user_id) == needle
                or (a.first_name or "").lower() == needle
                or a.phone.lstrip("+").endswith(needle)
            ):
                selected = a
                break
        if not selected:
            console.print(f"[red]Аккаунт '{target}' не найден.[/red]")
            return
    else:
        console.print("\n[bold cyan]Выберите аккаунт для получения кода входа:[/bold cyan]")
        for i, a in enumerate(accounts, start=1):
            console.print(f"  {i}. {a.display_name} ({a.first_name or '—'}, +{a.phone.lstrip('+')})")
        choice = click.prompt("Номер аккаунта", type=int, default=1)
        if 1 <= choice <= len(accounts):
            selected = accounts[choice - 1]
        else:
            console.print("[red]Неверный выбор.[/red]")
            return

    console.print(f"[cyan]🔍 Подключение к {selected.display_name} (+{selected.phone.lstrip('+')}) и чтение кода из Telegram...[/cyan]")
    max_age = 0 if any_age else 1800
    res = asyncio.run(fetch_telegram_service_code(selected, max_age_seconds=max_age))
    if res:
        code, dt, full_msg = res
        console.print(f"\n[bold green]🔑 Код подтверждения для {selected.display_name}: [bold yellow]{code}[/bold yellow] (от {dt.strftime('%H:%M:%S UTC')})[/bold green]")
        if not no_telegram:
            notifier = TelegramNotifier()
            text = (
                f"🔑 <b>Код входа в Telegram для {html.escape(selected.display_name)}</b>\n\n"
                f"Код: <code>{code}</code>\n"
                f"Время: <i>{dt.strftime('%d.%m %H:%M:%S UTC')}</i>\n"
                f"Телефон: <code>+{selected.phone.lstrip('+')}</code>"
            )
            notifier.send_message(text)
    else:
        console.print(f"[yellow]⚠️ Свежих кодов для {selected.display_name} в чате Telegram не найдено (запросите вход в приложении).[/yellow]")


@cli.command("qr-login")
@click.argument("target")
@click.argument("token_or_url")
def cmd_qr_login(target: str, token_or_url: str) -> None:
    """Мгновенно авторизовать Telegram Desktop по QR-ссылке (tg://login?token=...) без SIM-карты."""
    from telegpixel.ayugram_importer import accept_qr_login_token

    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    needle = target.strip().lstrip("@").lower()
    selected = next(
        (a for a in accounts if a.username.lower() == needle or (a.first_name or "").lower() == needle or str(a.user_id) == needle),
        None,
    )
    if not selected:
        console.print(f"[red]Аккаунт '{target}' не найден.[/red]")
        return

    console.print(f"[cyan]Подтверждение QR-входа для {selected.display_name}...[/cyan]")
    try:
        ok = asyncio.run(accept_qr_login_token(selected, token_or_url))
        if ok:
            console.print(f"[bold green]✅ Новое устройство успешно авторизовано в аккаунте {selected.display_name}![/bold green]")
        else:
            console.print("[red]❌ Не удалось подтвердить QR-токен.[/red]")
    except Exception as e:
        console.print(f"[red]❌ Ошибка QR-входа: {e}[/red]")


@cli.command("export-tdata")
@click.option("-o", "--output", "output_path", default="data/ayugram_sessions_backup.zip", show_default=True, help="Путь к результирующему ZIP-архиву.")
@click.option("-p", "--password", default=None, help="Пароль для AES-256 шифрования архива (по умолчанию без пароля).")
@click.option("--send-telegram", is_flag=True, help="Отправить готовый архив в Telegram-бот.")
@click.option("--no-portable", is_flag=True, help="Не генерировать отдельные portable папки для каждого аккаунта.")
def cmd_export_tdata(output_path: str, password: Optional[str], send_telegram: bool, no_portable: bool) -> None:
    """Экспортировать сессии в готовый архив для AyuGram со скриптом 1-клик восстановления."""
    import html
    from telegpixel.ayugram_importer import export_accounts_to_tdata_archive

    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    if not accounts:
        console.print("[red]❌ Нет активных аккаунтов для экспорта![/red]")
        sys.exit(1)

    out_file = Path(output_path)
    console.print(f"[cyan]📦 Генерация tdata и упаковка архива ({len(accounts)} аккаунтов)...[/cyan]")
    res_path = asyncio.run(
        export_accounts_to_tdata_archive(
            accounts,
            out_file,
            password=password,
            include_portable=not no_portable,
        )
    )
    console.print(f"[bold green]✅ Готовый архив сессий создан: {res_path} ({res_path.stat().st_size} байт)[/bold green]")
    console.print("[dim]В архиве: папка 'tdata' со всеми аккаунтами + скрипт 'RESTORE_TO_AYUGRAM.bat' для 1 клика.[/dim]")

    if send_telegram:
        console.print("[cyan]📤 Отправка архива в личный Telegram...[/cyan]")
        notifier = TelegramNotifier()
        caption = (
            "📦 <b>Готовый архив сессий Telegram / AyuGram</b>\n\n"
            f"👥 Аккаунтов в архиве: <b>{len(accounts)}</b>\n\n"
            "💡 <b>Как применить на ПК:</b>\n"
            "1. Распакуйте архив.\n"
            "2. Запустите <code>RESTORE_TO_AYUGRAM.bat</code> — все аккаунты сразу откроются в AyuGram!\n"
            + (f"\n🔑 Пароль архива: <code>{html.escape(password)}</code>" if password else "\n🔓 Без пароля (личная доставка).")
        )
        ok = notifier.send_document(res_path, caption=caption)
        if ok:
            console.print("[bold green]✅ Архив успешно отправлен в Telegram![/bold green]")
        else:
            console.print("[yellow]⚠️ Не удалось отправить документ в Telegram (проверьте TELEGPIXEL_CHAT_ID).[/yellow]")


@cli.command("restore-tdata")
@click.option("-i", "--input", "input_file", default=None, help="Путь к ZIP архиву (если не указан, восстановит напрямую из data/accounts.json).")
@click.option(
    "-t",
    "--target-dir",
    default=str(Path.home() / "Documents" / "AyuGram" / "tdata"),
    show_default=True,
    help="Куда записать сессии AyuGram.",
)
@click.option("-p", "--password", default=None, help="Пароль для расшифровки архива.")
@click.option("--close-ayugram", "auto_close", is_flag=True, default=True, help="Автоматически закрыть AyuGram перед записью.")
def cmd_restore_tdata(
    input_file: Optional[str],
    target_dir: str,
    password: Optional[str],
    auto_close: bool,
) -> None:
    """Восстановить сессии всех аккаунтов в AyuGram (из архива или напрямую из accounts.json)."""
    from telegpixel.ayugram_importer import (
        close_ayugram,
        is_ayugram_running,
        restore_accounts_to_ayugram_directly,
        restore_tdata_archive,
    )

    tpath = Path(target_dir)

    if is_ayugram_running():
        if auto_close or click.confirm("AyuGram сейчас запущен. Закрыть его для безопасного восстановления?", default=True):
            close_ayugram()
        else:
            console.print("[red]Отменено: сначала закройте AyuGram.[/red]")
            return

    if input_file:
        in_path = Path(input_file)
        if not in_path.exists():
            console.print(f"[red]❌ Файл не найден: {in_path}[/red]")
            sys.exit(1)
        console.print(f"[cyan]📂 Восстановление сессий из архива {in_path.name}...[/cyan]")
        slots = restore_tdata_archive(
            in_path,
            target_tdata_path=tpath,
            password=password,
            close_ayugram_first=False,
        )
    else:
        console.print("[cyan]📂 Прямое восстановление всех сессий из data/accounts.json в AyuGram...[/cyan]")
        mgr = AccountManager()
        accounts = mgr.list_accounts(enabled_only=True)
        if not accounts:
            console.print("[red]❌ В data/accounts.json нет активных аккаунтов![/red]")
            sys.exit(1)
        slots = asyncio.run(
            restore_accounts_to_ayugram_directly(
                accounts,
                target_tdata_path=tpath,
                close_ayugram_first=False,
            )
        )

    console.print(f"[bold green]✅ Успешно восстановлено в AyuGram активных аккаунтов: {len(slots)}[/bold green]")
    for s_idx, uid, dc, _ in slots:
        console.print(f"  • Слот #{s_idx}: [bold]User ID {uid}[/bold] (DC{dc})")
    console.print("\n[bold cyan]🎉 Готово! Теперь запустите AyuGram — все аккаунты появятся автоматически.[/bold cyan]")


@cli.command("backup")
@click.option("--send-telegram", is_flag=True, help="Отправить файл бэкапа и сводку в Telegram-бот.")
@click.option("--export-tdata", is_flag=True, help="Также экспортировать готовый архив tdata для AyuGram.")
def cmd_backup(send_telegram: bool, export_tdata: bool) -> None:
    """Создать локальный бэкап всех аккаунтов и сессий."""
    import html
    from telegpixel.ayugram_importer import export_accounts_to_tdata_archive

    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=True)
    export_data = mgr.export_secret_json()
    out_file = PROJECT_ROOT / "data" / "accounts_backup.json"
    out_file.write_text(export_data, encoding="utf-8")
    console.print(f"[bold green]✅ Бэкап всех {len(accounts)} аккаунтов сохранён в {out_file}[/bold green]")

    tdata_zip = None
    if export_tdata:
        tdata_zip = PROJECT_ROOT / "data" / "ayugram_sessions_backup.zip"
        console.print("[cyan]📦 Экспорт tdata архива для AyuGram...[/cyan]")
        asyncio.run(export_accounts_to_tdata_archive(accounts, tdata_zip))
        console.print(f"[bold green]✅ Архив tdata сохранён: {tdata_zip}[/bold green]")

    if send_telegram:
        notifier = TelegramNotifier()
        lines = [
            "🔐 <b>Бэкап аккаунтов и сессий TelegPixel</b>",
            f"Всего подключено аккаунтов: <b>{len(accounts)}</b>\n",
        ]
        for i, a in enumerate(accounts, 1):
            lines.append(f"{i}. <b>{html.escape(a.display_name)}</b> (+{a.phone.lstrip('+')})")
        lines.append("\n💡 <i>Сессии надёжно сохранены в GitHub Secrets и зашифрованном кэше.</i>")
        lines.append("<i>Для получения готового архива AyuGram напишите /tdata боту.</i>")
        notifier.send_message("\n".join(lines))

        if tdata_zip and tdata_zip.exists():
            notifier.send_document(
                tdata_zip,
                caption="📦 <b>Архив AyuGram tdata со всеми сессиями</b>",
            )


if __name__ == "__main__":
    cli()
