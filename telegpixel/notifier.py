"""
Telegram Bot API notification & command handler for TelegPixel (@pixelfarmik143714bot).
Sends rich per-account coin reports modeled after ThundrCoin with anti-flag protections.
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

import telegpixel.env_loader  # noqa: F401
from telegpixel.accounts import AccountRecord
from telegpixel.parser import CheckinStatus
from telegpixel.runner import AccountRunResult

logger = logging.getLogger("telegpixel.notifier")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOT_STATE_FILE = PROJECT_ROOT / "data" / "telegram_bot_state.json"

STANDARD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _coin_word_ru(n: int) -> str:
    n_abs = abs(int(n))
    if 11 <= (n_abs % 100) <= 14:
        return "монет"
    rem = n_abs % 10
    if rem == 1:
        return "монета"
    if 2 <= rem <= 4:
        return "монеты"
    return "монет"


class TelegramNotifier:
    """Formats and sends multi-account check-in & coin balance reports to Telegram."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        tz_name: Optional[str] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        self.bot_token = (
            bot_token
            or os.getenv("TELEGPIXEL_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or ""
        ).strip()
        self.chat_id = str(
            chat_id
            or os.getenv("TELEGPIXEL_CHAT_ID")
            or os.getenv("TELEGRAM_CHAT_ID")
            or ""
        ).strip()
        self.tz_name = tz_name or os.getenv("APP_TIMEZONE", "Europe/Berlin")
        self.state_file = state_file or DEFAULT_BOT_STATE_FILE
        self.verify_cost = int(os.getenv("VERIFY_COST_COINS", "30"))
        self.pixel_cost = int(os.getenv("PIXEL_COST_COINS", "50"))

        try:
            self.tz = ZoneInfo(self.tz_name)
        except Exception:
            self.tz = timezone.utc

        self._last_send_ts = 0.0
        self.min_send_interval = 2.0

    def _load_bot_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_bot_state(self, updates: Dict[str, Any]) -> None:
        try:
            st = self._load_bot_state()
            st.update(updates)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(st, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not save bot state: {e}")

    @staticmethod
    def get_environment_label() -> str:
        if os.getenv("GITHUB_ACTIONS") == "true":
            return "☁️ <i>Среда: GitHub Actions</i>"
        return "💻 <i>Среда: Локальный запуск</i>"

    def auto_discover_chat_id(self) -> Optional[str]:
        """Attempt to discover chat_id from recent /start messages if TELEGRAM_CHAT_ID is empty."""
        if self.chat_id:
            return self.chat_id
        st = self._load_bot_state()
        if st.get("chat_id"):
            self.chat_id = str(st["chat_id"])
            return self.chat_id
        if not self.bot_token:
            return None
        if os.getenv("GITHUB_ACTIONS") == "true":
            return None
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params={"timeout": 0},
                headers={"User-Agent": STANDARD_USER_AGENT},
                timeout=8,
            )
            data = resp.json()
            if data.get("ok") and data.get("result"):
                for upd in reversed(data["result"]):
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    if chat.get("id"):
                        self.chat_id = str(chat["id"])
                        self._save_bot_state({"chat_id": self.chat_id})
                        return self.chat_id
        except Exception as e:
            logger.debug(f"Chat ID auto-discovery failed: {e}")
        return None

    def format_report(
        self,
        results: List[AccountRunResult],
        stats_only: bool = False,
        dry_run: bool = False,
        now_dt: Optional[datetime] = None,
    ) -> str:
        now_local = (now_dt or datetime.now(timezone.utc)).astimezone(self.tz)
        now_str = now_local.strftime("%d.%m.%Y %H:%M %Z")

        total_accs = len(results)
        claimed_cnt = sum(1 for r in results if r.status == CheckinStatus.CLAIMED)
        cooldown_cnt = sum(1 for r in results if r.status == CheckinStatus.COOLDOWN)
        failed_cnt = sum(1 for r in results if r.status == CheckinStatus.FAILED)
        earned_today = sum(r.coins_earned for r in results if r.status == CheckinStatus.CLAIMED)

        total_coins = sum(r.balance or 0 for r in results)
        total_usd = sum(
            r.usd_value if r.usd_value is not None else round((r.balance or 0) * 0.04, 2)
            for r in results
        )

        if stats_only or dry_run:
            title = "📊 <b>GEMINI PIXEL — БАЛАНС МОНЕТОК</b>"
        else:
            title = "🎓 <b>GEMINI PIXEL — ЕЖЕДНЕВНЫЙ ЧЕКИН</b>"

        lines: List[str] = [
            title,
            f"<i>{now_str}</i>\n",
            f"👥 Всего аккаунтов: <b>{total_accs}</b>",
        ]

        if not stats_only and not dry_run:
            done_cnt = claimed_cnt + cooldown_cnt
            if done_cnt == total_accs and total_accs > 0 and failed_cnt == 0:
                lines.append(
                    f"📅 <b>Чекин сегодня:</b> ✅ Выполнен на всех ({done_cnt}/{total_accs})"
                )
            elif failed_cnt > 0:
                lines.append(
                    f"📅 <b>Чекин сегодня:</b> ⚠️ Успешно {done_cnt}/{total_accs} (ошибок: {failed_cnt})"
                )
            else:
                lines.append(f"📅 <b>Чекин сегодня:</b> {done_cnt}/{total_accs}")

        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>Монетки по аккаунтам:</b>")

        # Sort by coin balance descending so richest accounts appear at top
        sorted_results = sorted(
            results,
            key=lambda r: (r.balance if r.balance is not None else -1, r.account.display_name.lower()),
            reverse=True,
        )

        for idx, res in enumerate(sorted_results, start=1):
            label = html.escape(res.account.display_name)
            bal = res.balance
            usd = res.usd_value if res.usd_value is not None else (round(bal * 0.04, 2) if bal is not None else None)
            bal_str = f"<b>{bal} {_coin_word_ru(bal)}</b>" if bal is not None else "<b>? монет</b>"
            usd_str = f" (${usd:.2f})" if usd is not None else ""

            # Milestone badge (/pixel = 50, /verify = 30)
            goal_badge = ""
            if bal is not None:
                if bal >= self.pixel_cost:
                    goal_badge = " 🎯<i>(хватает на /pixel!)</i>"
                elif bal >= self.verify_cost:
                    goal_badge = " 🔹<i>(хватает на /verify)</i>"

            streak_part = f", 🔥 {res.streak_days} дн." if res.streak_days else ""

            if res.status == CheckinStatus.CLAIMED:
                status_part = f"✅ <code>+{res.coins_earned}</code>{streak_part}"
            elif res.status == CheckinStatus.COOLDOWN:
                status_part = f"⏳ <i>уже забран</i>{streak_part}"
            elif res.status == CheckinStatus.CHECKED:
                status_part = f"🔎 <i>проверен</i>{streak_part}"
            else:
                short_err = html.escape((res.error_message or "ошибка")[:45])
                status_part = f"❌ <i>{short_err}</i>"

            lines.append(f"{idx}. <code>{label}</code> — {bal_str}{usd_str} | {status_part}{goal_badge}")

        lines.append("━━━━━━━━━━━━━━━━━━━")
        earned_suffix = f" (<b>+{earned_today}</b> за прогон)" if earned_today > 0 else ""
        lines.append(
            f"🏦 <b>Общий баланс:</b> <b>{total_coins} {_coin_word_ru(total_coins)}</b> (${total_usd:.2f}){earned_suffix}"
        )

        if sorted_results and sorted_results[0].balance is not None:
            top = sorted_results[0]
            top_bal = top.balance or 0
            rem_pixel = max(0, self.pixel_cost - top_bal)
            rem_note = (
                "готов к /pixel! 🎉"
                if rem_pixel == 0
                else f"до /pixel ещё {rem_pixel}"
            )
            lines.append(
                f"🏆 <b>Топ аккаунт:</b> <code>{html.escape(top.account.display_name)}</code> ({top_bal} {_coin_word_ru(top_bal)} — {rem_note})"
            )

        ready_pixel_cnt = sum(1 for r in results if (r.balance or 0) >= self.pixel_cost)
        ready_verify_cnt = sum(1 for r in results if (r.balance or 0) >= self.verify_cost)
        if ready_pixel_cnt > 0 or ready_verify_cnt > 0:
            lines.append(
                f"🎁 <b>Доступно услуг:</b> /pixel ({self.pixel_cost}+): <b>{ready_pixel_cnt}</b> акк. | /verify ({self.verify_cost}+): <b>{ready_verify_cnt}</b> акк."
            )

        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append(self.get_environment_label())
        return "\n".join(lines)

    def should_send_report(self, results: List[AccountRunResult], smart_notify: bool = False) -> bool:
        """
        In smart_notify mode (used for frequent 8h cron runs), skip duplicate cooldown-only reports
        unless at least 20 hours have elapsed since the last sent report.
        Always send immediately if any account claimed coins or encountered an error.
        """
        if not smart_notify:
            return True
        has_claimed = any(r.status == CheckinStatus.CLAIMED for r in results)
        has_failed = any(r.status == CheckinStatus.FAILED for r in results)
        if has_claimed or has_failed:
            return True

        st = self._load_bot_state()
        last_ts = float(st.get("last_report_sent_ts") or 0.0)
        if (time.time() - last_ts) >= 20 * 3600:
            return True

        logger.info("Все аккаунты уже на кулдауне, а сводка за сегодня уже отправлялась — пропуск дубля.")
        return False

    def send_message(self, html_text: str, mark_report_sent: bool = False) -> bool:
        if not self.bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN не задан — пропуск отправки уведомления.")
            return False
        target_chat = self.chat_id or self.auto_discover_chat_id()
        if not target_chat:
            logger.warning("TELEGRAM_CHAT_ID не задан и не найден через getUpdates — напишите /start боту.")
            return False

        is_testing = bool(os.getenv("PYTEST_CURRENT_TEST"))
        if not is_testing:
            elapsed = time.time() - getattr(self, "_last_send_ts", 0.0)
            if elapsed < self.min_send_interval:
                time.sleep(self.min_send_interval - elapsed)

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": target_chat,
                    "text": html_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                headers={"User-Agent": STANDARD_USER_AGENT},
                timeout=15,
            )
            self._last_send_ts = time.time()
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info(f"Отчёт успешно отправлен в Telegram (chat_id={target_chat}).")
                if mark_report_sent:
                    self._save_bot_state({"last_report_sent_ts": time.time()})
                return True
            if resp.status_code == 429 and not is_testing:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                logger.warning(f"Telegram FloodWait ({retry_after}s)...")
                time.sleep(retry_after + 1.0)
            logger.error(f"Telegram API error ({resp.status_code}): {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

    def process_pending_commands(self, accounts: List[AccountRecord]) -> None:
        """Reply to /start or /stats commands sent to @pixelfarmik143714bot locally."""
        if not self.bot_token:
            return
        # Avoid polling getUpdates from rotating GitHub Actions datacenter IPs to prevent bot freeze
        if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("TELEGPIXEL_POLL_IN_CI", "").lower() != "true":
            return
        st = self._load_bot_state()
        offset = st.get("last_update_id")
        params: Dict[str, Any] = {"timeout": 0}
        if isinstance(offset, int):
            params["offset"] = offset + 1

        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params=params,
                headers={"User-Agent": STANDARD_USER_AGENT},
                timeout=8,
            )
            data = resp.json()
            if not data.get("ok"):
                return
            updates = data.get("result") or []
            if not updates:
                return

            max_upd_id = offset or 0
            for upd in updates:
                upd_id = int(upd.get("update_id", 0))
                if upd_id > max_upd_id:
                    max_upd_id = upd_id
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                cid = str(chat.get("id") or "")
                text = (msg.get("text") or "").strip().lower()
                if not cid or not text.startswith("/"):
                    continue
                if not self.chat_id:
                    self.chat_id = cid
                    self._save_bot_state({"chat_id": cid})
                if cid != str(self.chat_id):
                    continue

                if text.startswith(("/start", "/help")):
                    reply = (
                        "👋 <b>Привет! Я бот отчётов TelegPixel (@pixelfarmik143714bot).</b>\n\n"
                        "Каждый день я автоматически захожу со всех твоих аккаунтов в "
                        "<b>Gemini Pro Automation Bot 🎓</b> (<code>@sheeridverifier_bot</code>), "
                        "пишу <code>/checkin</code> и присылаю сюда сводку по монеткам на каждом аккаунте.\n\n"
                        f"📌 <b>Твой Chat ID:</b> <code>{cid}</code>\n"
                        f"👥 <b>Подключено аккаунтов:</b> <b>{len(accounts)}</b>"
                    )
                    self.send_message(reply)
                elif text.startswith(("/stats", "/balance", "/coins")):
                    cached_results = [
                        AccountRunResult(
                            account=a,
                            status=CheckinStatus.CHECKED,
                            balance=a.balance,
                            usd_value=a.usd_value,
                            streak_days=a.streak_days,
                        )
                        for a in accounts
                    ]
                    self.send_message(self.format_report(cached_results, stats_only=True))

            self._save_bot_state({"last_update_id": max_upd_id})
        except Exception as e:
            logger.debug(f"Error processing pending bot updates: {e}")
