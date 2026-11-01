#!/usr/bin/env python3
"""
Command Line Interface (CLI) for TelegPixel Daily Check-in & Coin Tracker.
Automates daily /checkin in Gemini Pro Automation Bot (@sheeridverifier_bot)
and sends per-account coin summaries via Telegram Bot API (@PixellinnoBobmbardinnoBOT).
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
def cmd_run(account_name: Optional[str], dry_run: bool, no_telegram: bool) -> None:
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

    if not no_telegram:
        report_html = notifier.format_report(results, dry_run=dry_run)
        notifier.send_message(report_html)

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
        notifier.send_message(notifier.format_report(results, stats_only=True))


@cli.command("list-accounts")
def cmd_list_accounts() -> None:
    """Показать список подключённых Telegram-аккаунтов."""
    mgr = AccountManager()
    accounts = mgr.list_accounts(enabled_only=False)
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


if __name__ == "__main__":
    cli()
