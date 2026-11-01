from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from telegpixel.accounts import AccountManager, AccountRecord
from telegpixel.notifier import TelegramNotifier
from telegpixel.parser import (
    CheckinStatus,
    extract_balance_info,
    is_broadcast_or_unrelated,
    parse_checkin_reply,
    parse_profile_reply,
)
from telegpixel.runner import AccountRunResult


def test_parse_checkin_english_success():
    msg = (
        "🎉 Attendance success!\n"
        "═══════════════════════\n\n"
        "💎 +1 coin | $0.04\n"
        "💰 Balance: 27 coin | $1.08\n"
        "📅 Date series: 2 day\n"
        "═══════════════════════"
    )
    res = parse_checkin_reply(msg)
    assert res.status == CheckinStatus.CLAIMED
    assert res.coins_earned == 1
    assert res.balance == 27
    assert res.usd_value == 1.08
    assert res.streak_days == 2


def test_parse_checkin_russian_success():
    msg = (
        "🎉 посещаемость успех!\n"
        "═══════════════════════\n\n"
        "💎 +1 монета | $0.04\n"
        "💰 Баланс: 29 монета | $1.16\n"
        "📅 Серия дат: 1 день\n"
        "═══════════════════════"
    )
    res = parse_checkin_reply(msg)
    assert res.status == CheckinStatus.CLAIMED
    assert res.coins_earned == 1
    assert res.balance == 29
    assert res.usd_value == 1.16
    assert res.streak_days == 1


def test_parse_checkin_cooldown():
    msg_en = "⚠️ You have already checked in today! Please come back tomorrow."
    res_en = parse_checkin_reply(msg_en)
    assert res_en.status == CheckinStatus.COOLDOWN
    assert res_en.coins_earned == 0
    assert res_en.balance is None

    msg_ru = "⚠️ Вы уже отметились сегодня! Приходите завтра."
    res_ru = parse_checkin_reply(msg_ru)
    assert res_ru.status == CheckinStatus.COOLDOWN
    assert res_ru.coins_earned == 0


def test_parse_unexpected_prompt():
    msg = (
        "📎 Send verification link\n"
        "═══════════════════════\n\n"
        "🪐 Please send your SheerID link 🪐"
    )
    res = parse_checkin_reply(msg)
    assert res.status == CheckinStatus.FAILED
    assert res.is_unexpected_prompt is True


def test_parse_profile_me_en_and_ru():
    me_ru = (
        "👤 Информация об аккаунте\n"
        "═══════════════════════\n\n"
        "🆔 ИДЕНТИФИКАТОР: 100001\n"
        "👤 Имя пользователя: @test_user_ru\n"
        "📝 Имя: -\n"
        "🏷️ Роль: Пользователь\n"
        "═══════════════════════\n"
        "💰 Баланс: 29 монета | $1.16"
    )
    p_ru = parse_profile_reply(me_ru)
    assert p_ru.status == CheckinStatus.CHECKED
    assert p_ru.balance == 29
    assert p_ru.usd_value == 1.16

    me_en = (
        "👤 Account information\n"
        "═══════════════════════\n\n"
        "🆔 ID: 100002\n"
        "👤 Username: @test_user_en\n"
        "═══════════════════════\n"
        "💰 Balance: 27 coin | $1.08"
    )
    p_en = parse_profile_reply(me_en)
    assert p_en.status == CheckinStatus.CHECKED
    assert p_en.balance == 27
    assert p_en.usd_value == 1.08


def test_extract_balance_from_start_ru():
    start_ru = (
        "👋 Добро пожаловать - вернись с Gemini Pro Automation Bot 🎓!\n\n"
        "💰 Текущее количество монет: 31 монета | $1.24 | Контакт Админ"
    )
    bal, usd, streak = extract_balance_info(start_ru)
    assert bal == 31
    assert usd == 1.24
    assert streak is None


def test_broadcast_filter():
    promo = "🔥 SHOP.APPMMO.COM – YOUR ULTIMATE DIGITAL PRODUCTS & MMO MARKETPLACE 🔥"
    assert is_broadcast_or_unrelated(promo) is True
    assert is_broadcast_or_unrelated("🎉 Attendance success!\n💰 Balance: 27 coin | $1.08") is False


def test_account_manager_and_report_formatting(tmp_path: Path):
    acc_file = tmp_path / "accounts.json"
    st_file = tmp_path / "state.json"

    mgr = AccountManager(accounts_file=acc_file, state_file=st_file)
    rec1 = AccountRecord(user_id=111, username="alpha_acc", session="sess1", balance=30, usd_value=1.20, streak_days=1)
    rec2 = AccountRecord(user_id=222, username="beta_acc", session="sess2", balance=28, usd_value=1.12, streak_days=3)
    mgr.upsert_account(rec1)
    mgr.upsert_account(rec2)
    mgr.save_accounts()
    mgr.save_state()

    mgr2 = AccountManager(accounts_file=acc_file, state_file=st_file)
    loaded = mgr2.list_accounts()
    assert len(loaded) == 2
    assert json.loads(mgr2.export_secret_json())[0]["username"] == "alpha_acc"

    notifier = TelegramNotifier(bot_token="dummy", chat_id="123")
    report = notifier.format_report(
        [
            AccountRunResult(account=rec1, status=CheckinStatus.CLAIMED, coins_earned=1, balance=30, usd_value=1.20, streak_days=1),
            AccountRunResult(account=rec2, status=CheckinStatus.COOLDOWN, coins_earned=0, balance=28, usd_value=1.12, streak_days=3),
        ],
        now_dt=datetime(2026, 11, 1, 7, 15, tzinfo=timezone.utc),
    )
    assert "@alpha_acc" in report
    assert "@beta_acc" in report
    assert "30 монет" in report
    assert "28 монет" in report
    assert "58 монет" in report
    assert "хватает на /verify" in report


def test_smart_notify_throttling(tmp_path: Path):
    bot_state = tmp_path / "bot_state.json"
    notifier = TelegramNotifier(bot_token="dummy", chat_id="123", state_file=bot_state)
    acc = AccountRecord(user_id=111, username="alpha_acc", session="sess1", balance=30)

    cooldown_only = [AccountRunResult(account=acc, status=CheckinStatus.COOLDOWN, balance=30)]
    claimed_run = [AccountRunResult(account=acc, status=CheckinStatus.CLAIMED, coins_earned=1, balance=31)]

    # First cooldown run with no prior report -> should send
    assert notifier.should_send_report(cooldown_only, smart_notify=True) is True
    notifier._save_bot_state({"last_report_sent_ts": 9999999999.0})

    # Recent report already sent & only cooldown -> skip duplicate spam
    assert notifier.should_send_report(cooldown_only, smart_notify=True) is False
    # Claimed coins -> always send immediately even if recent report existed!
    assert notifier.should_send_report(claimed_run, smart_notify=True) is True
    # Manual run (smart_notify=False) -> always send!
    assert notifier.should_send_report(cooldown_only, smart_notify=False) is True
